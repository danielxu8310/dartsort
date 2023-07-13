from pathlib import Path

import h5py
import numpy as np
import torch
from dartsort.util.data_util import SpikeDataset
from dartsort.util.multiprocessing_util import get_pool
from spikeinterface.core.recording_tools import get_chunk_with_margin
from tqdm.auto import tqdm


class BasePeeler(torch.nn.Module):
    """Base class for peeling operations (subtraction, deconv, etc)

    Subtraction, deconv, and other things like just grabbing waveforms
    and featurizing a preexisting spike train (implemented with optional
    additional peeling of preexisting templates in grab.py) share a lot
    of logic for featurization and parallelization and data saving/loading.
    This class handles all of that stuff so that it can be shared.
    """

    peel_kind = ""

    # subclasses which need to fit models other than the post-peeling
    # featurization_pipeline should set this to True (and False after
    # they fit the pipeline)
    peeling_needs_fit = False

    def __init__(
        self,
        recording,
        channel_index,
        featurization_pipeline,
        chunk_length_samples=30_000,
        chunk_margin_samples=0,
        n_seconds_fit=40,
        fit_subsampling_random_state=0,
        device=None,
    ):
        assert recording.get_num_channels() == channel_index.shape[0]
        if recording.get_num_segments() > 1:
            raise ValueError(
                "Peeling does not yet support multi-segment recordings."
            )
        super().__init__()
        self.recording = recording
        self.chunk_length_samples = chunk_length_samples
        self.chunk_margin_samples = chunk_margin_samples
        self.n_seconds_fit = n_seconds_fit
        self.fit_subsampling_random_state = np.random.default_rng(
            fit_subsampling_random_state
        )
        self.device = device
        self.register_buffer("channel_index", channel_index)
        self.add_module("featurization_pipeline", featurization_pipeline)

        # subclasses can add to this list
        # for each dataset in this list, an output dataset in the
        # hdf5 (of .peel()) will be created with this dtype and with
        # shape (N_spikes, *shape_per_spike)
        # datasets will also be created corresponding to features
        # in featurization_pipeline
        self.out_datasets = [
            SpikeDataset(name="times", shape_per_spike=(), dtype=int),
            SpikeDataset(name="channels", shape_per_spike=(), dtype=int),
        ]
        for transformer in self.featurization_pipeline.transformers:
            if transformer.is_featurizer:
                self.out_datasets.append(transformer.spike_dataset)

        # subclasses can append to this if they want to store more fixed
        # arrays in the output h5 file
        self.fixed_output_data = [
            ("fs", self.recording.get_sampling_frequency()),
            ("geom", self.recording.get_channel_locations()),
            ("channel_index", self.channel_index),
        ]

    # -- main functions for users to call
    # in practice users will interact with the functions `subtract(...)` in
    # subtract.py and similar functions in the other .py files here, but these
    # are the main API methods for this class

    def load_or_fit_and_save_models(self, save_folder):
        """Load fitted models from save_folder if possible, or fit and save

        If the peeler has models that need to be trained, this function ensures
        that the models are fitted and that their fitted parameters are saved
        to `save_folder`
        """
        if self.needs_fit:
            self.load_models(save_folder)
        if self.needs_fit:
            self.fit_models()
            self.save_models(save_folder)
        assert not self.needs_fit

    def peel(
        self,
        output_hdf5_filename,
        chunk_starts_samples=None,
        n_jobs=0,
        overwrite=False,
        residual_filename=None,
        show_progress=True,
    ):
        """Run the full (already fitted) peeling and featurization pipeline

        This gathers all results into an hdf5 file, resuming from where
        it left off in that file if overwrite=False.
        """
        if self.needs_fit:
            raise ValueError("Peeler needs to be fitted before peeling.")

        (
            last_chunk_start,
            output_h5,
            h5_spike_datasets,
            save_residual,
            residual_file,
        ) = self.initialize_files(
            output_hdf5_filename,
            residual_filename=residual_filename,
            overwrite=overwrite,
        )

        # figure out which chunks to process, and exit early if already done
        if chunk_starts_samples is None:
            T_samples = self.recording.get_num_samples()
            chunk_starts_samples = range(
                0, T_samples, self.chunk_length_samples
            )
        n_chunks_orig = len(chunk_starts_samples)
        chunk_starts_samples = [
            start for start in chunk_starts_samples if start > last_chunk_start
        ]
        n_chunks = len(chunk_starts_samples)
        if not chunk_starts_samples:
            output_h5.close()

        # main peeling loop
        # wrap in try/finally to ensure file handles get closed if there
        # is some unforseen error
        try:
            had_grad = torch.is_grad_enabled()
            n_jobs, Executor, context, rank_queue = get_pool(
                n_jobs, with_rank_queue=True
            )
            with Executor(
                max_workers=n_jobs,
                mp_context=context,
                initializer=_peeler_process_init,
                initargs=(self, rank_queue, save_residual),
            ) as pool:
                results = zip(
                    chunk_starts_samples,
                    pool.map(_peeler_process_job, chunk_starts_samples),
                )
                if show_progress:
                    n_sec_chunk = (
                        self.chunk_length_samples
                        / self.recording.get_sampling_frequency()
                    )
                    results = tqdm(
                        results,
                        total=n_chunks_orig,
                        initial=n_chunks_orig - n_chunks,
                        smoothing=0.01,
                        desc=f"{self.peel_kind} {n_sec_chunk:.1f}s/it [spk/it=%%%]",
                    )

                batch_count = 0
                n_spikes = 0
                for chunk_start_samples, result in results:
                    n_new_spikes = self.gather_chunk_result(
                        n_spikes,
                        chunk_start_samples,
                        result,
                        h5_spike_datasets,
                        output_h5,
                        residual_file,
                    )
                    batch_count += 1
                    n_spikes += n_new_spikes
                    if show_progress:
                        results.set_description(
                            f"{self.peel_kind} {n_sec_chunk:.1f}s/batch "
                            f"[spk/batch={n_spikes / batch_count:0.1f}]"
                        )
        finally:
            output_h5.close()
            if save_residual:
                residual_file.close()
            torch.set_grad_enabled(had_grad)

        return output_hdf5_filename

    # -- methods for subclasses to override

    def peel_chunk(
        self,
        traces,
        chunk_start_samples=0,
        left_margin=0,
        right_margin=0,
        return_residual=False,
    ):
        # subclasses should implement this method

        # they should return a dictionary with keys:
        #  - n_spikes
        #  - collisioncleaned_waveforms
        #  - times (relative to start of traces)
        #  - channels
        #  - residual if requested
        #  - arbitrary subclass-specific stuff which should have keys in out_datasets

        # data for spikes in the margin should not be returned
        # these spikes will have been processed by the neighboring chunk

        raise NotImplementedError

    def fit_peeler(self):
        # subclasses should override if they need to fit models for peeling
        assert not self.peeling_needs_fit

    # -- utility methods which users likely won't touch

    def featurize_collisioncleaned_waveforms(
        self, collisioncleaned_waveforms, max_channels
    ):
        waveforms, features = self.featurization_pipeline(
            collisioncleaned_waveforms, max_channels=max_channels
        )
        return features

    def process_chunk(self, chunk_start_samples):
        """Grab, peel, and featurize a chunk, returning a dict of numpy arrays

        Main function called in peeling workers
        """
        chunk, left_margin, right_margin = get_chunk_with_margin(
            self.recording._recording_segments[0],
            start_frame=chunk_start_samples,
            end_frame=chunk_start_samples + self.chunk_length_samples,
            channel_indices=None,
            margin=self.chunk_margin_samples,
        )
        chunk = torch.tensor(chunk, device=self.device)
        peel_result = self.peel_chunk(
            chunk,
            chunk_start_samples=chunk_start_samples,
            left_margin=left_margin,
            right_margin=right_margin,
        )
        features = self.featurize_collisioncleaned_waveforms(
            peel_result["collisioncleaned_waveforms"], peel_result["channels"]
        )
        assert not any(k in features for k in peel_result)
        chunk_result = {**peel_result, **features}
        chunk_result = {
            k: v.cpu().numpy() if torch.is_tensor(v) else v
            for k, v in chunk_result.items()
        }
        return chunk_result

    def gather_chunk_result_to_h5(
        self,
        cur_n_spikes,
        chunk_start_samples,
        chunk_result,
        h5_spike_datasets,
        output_h5,
        residual_file,
    ):
        # write stuff
        output_h5["last_chunk_start"][()] = chunk_start_samples
        n_new_spikes = chunk_result["n_spikes"]

        if residual_file is not None:
            chunk_result["residual"].tofile(residual_file)

        if not n_new_spikes:
            return 0

        for ds in self.out_datasets:
            h5_spike_datasets[ds.name].resize(
                cur_n_spikes + n_new_spikes, axis=0
            )
            h5_spike_datasets[ds.name][cur_n_spikes:] = chunk_result[ds.name]

        return n_new_spikes

    @property
    def needs_fit(self):
        return self.peeling_needs_fit or self.featurization_pipeline.needs_fit

    def fit_models(self):
        if self.peeling_needs_fit:
            self.fit_peeler()
        self.fit_featurization_pipeline()
        assert not self.needs_fit

    def fit_featurization_pipeline(self):
        # make copy of self with featurization_pipeline
        # run copy.peel() on random (with seed) subset of chunks
        raise NotImplementedError

    def save_models(self, save_folder):
        torch.save(
            self.featurization_pipeline,
            Path(save_folder) / "featurization_pipeline",
        )

    def load_models(self, save_folder):
        self.featurization_pipeline = torch.load(
            Path(save_folder) / "featurization_pipeline"
        )

    def initialize_files(
        self,
        output_hdf5_filename,
        residual_filename=None,
        overwrite=False,
        chunk_size=1024,
    ):
        """Create, overwrite, or re-open output files"""
        output_hdf5_filename = Path(output_hdf5_filename)
        exists = output_hdf5_filename.exists()
        if exists and overwrite:
            output_hdf5_filename.unlink()
        elif exists:
            # exists and not overwrite
            output_h5 = h5py.File(output_hdf5_filename, "r+")
        else:
            # didn't exist, so overwrite does not matter
            output_h5 = h5py.File(output_hdf5_filename, "w")
            output_h5.create_dataset("last_chunk_start", data=0)
        last_chunk_start = output_h5["last_chunk_start"][()]

        # write some fixed arrays that are useful to have around
        for name, array in self.fixed_output_data:
            if name not in output_h5:
                output_h5.create_dataset(name, data=array)

        # create per-spike datasets
        # use chunks to support growing the dataset as we find spikes
        h5_spike_datasets = {}
        for ds in self.out_datasets:
            h5_spike_datasets[ds.name] = output_h5.require_dataset(
                ds.name,
                dtype=ds.dtype,
                shape=(None, *ds.shape),
                chunks=(chunk_size, *ds.shape),
                exact=True,
            )

        # residual file ignore/open/overwrite logic
        save_residual = residual_filename is not None
        residual_file = None
        if save_residual:
            residual_mode = "wb"
            if last_chunk_start > 0:
                residual_mode = "ab"
                assert Path(residual_filename).exists()
            residual_file = open(residual_filename, mode=residual_mode)

        return (
            last_chunk_start,
            output_h5,
            h5_spike_datasets,
            save_residual,
            residual_file,
        )


# -- helper functions and objects for parallelism


class ProcessContext:
    def __init__(self, peeler, device, save_residual):
        self.peeler = peeler
        self.device = device
        self.save_residual = save_residual


# this state will be set on each process
# it means that BasePeeler.peel() itself is not thread-safe but that's ok
_peeler_process_context = None


def _peeler_process_init(peeler, device, rank_queue):
    global _peeler_process_context
    # if we are not running in parallel, this state is restored
    # in the logic in .peel
    torch.set_grad_enabled(False)

    # figure out what device to work on
    my_rank = rank_queue.get()
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type == "cuda" and device.index is None:
        if torch.cuda.device_count() > 1:
            device = torch.device(
                "cuda", index=my_rank % torch.cuda.device_count()
            )

    # move peeler to device
    peeler.to(device)
    peeler.eval()

    # update process-local variables
    _peeler_process_context = ProcessContext(peeler, device)


def _peeler_process_job(chunk_start_samples):
    peeler = _peeler_process_context.peeler
    # by returning here, we are implicitly relying on pickle
    # we can replace this with cloudpickle or manual np.save if helpful
    return peeler.process_chunk(
        chunk_start_samples,
        return_residual=_peeler_process_context.save_residual,
    )
