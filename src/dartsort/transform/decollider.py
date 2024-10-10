import numpy as np
import torch
import torch.nn.functional as F
from dartsort.util import nn_util, spikeio
from dartsort.util.spiketorch import get_relative_index, reindex
from dartsort.util.waveform_util import regularize_channel_index, grab_main_channels
from torch.utils.data import Dataset, DataLoader, StackDataset, TensorDataset, BatchSampler, RandomSampler, WeightedRandomSampler
from dartsort.util.waveform_util import regularize_channel_index
from torch.utils.data import DataLoader, TensorDataset
from tqdm.auto import trange

from .transform_base import BaseWaveformDenoiser


class Decollider(BaseWaveformDenoiser):
    default_name = "decollider"

    def __init__(
        self,
        channel_index,
        geom,
        recording,
        hidden_dims=(256, 256),
        use_batchnorm=True,
        name=None,
        name_prefix="",
        exz_estimator="n3n",
        inference_kind="raw",
        batch_size=32,
        learning_rate=1e-3,
        epochs=25,
        channelwise_dropout_p=0.2,
        n_data_workers=0,
        with_conv_fullheight=False,
        sample_weighting=None,
    ):
        assert inference_kind in ("raw", "amortized")
        assert exz_estimator in ("n2n", "2n2", "n3n", "3n3")
        assert inference_kind in ("raw", "exz", "amortized")
        assert sample_weighting in (None, "kmeans")
        super().__init__(
            geom=geom, channel_index=channel_index, name=name, name_prefix=name_prefix
        )

        self.use_batchnorm = use_batchnorm
        self.exz_estimator = exz_estimator
        self.inference_kind = inference_kind
        self.hidden_dims = hidden_dims
        self.n_channels = len(geom)
        self.recording = recording
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.epochs = epochs
        self.channelwise_dropout_p = channelwise_dropout_p
        self.n_data_workers = n_data_workers
        self.with_conv_fullheight = with_conv_fullheight
        self.sample_weighting = sample_weighting

        self.model_channel_index_np = regularize_channel_index(
            geom=self.geom, channel_index=channel_index
        )
        self.register_buffer(
            "model_channel_index", torch.from_numpy(self.model_channel_index_np)
        )
        self.register_buffer(
            "relative_index",
            get_relative_index(self.channel_index, self.model_channel_index),
        )
        # suburban lawns -- janitor
        self.register_buffer(
            "irrelative_index",
            get_relative_index(self.model_channel_index, self.channel_index),
        )
        self._needs_fit = True

    def needs_fit(self):
        return self._needs_fit

    def get_mlp(self):
        return nn_util.get_waveform_mlp(
            self.spike_length_samples,
            self.model_channel_index.shape[1],
            self.hidden_dims,
            self.output_dim,
            use_batchnorm=self.use_batchnorm,
            channelwise_dropout_p=self.channelwise_dropout_p,
            separated_mask_input=True,
            return_initial_shape=True,
            initial_conv_fullheight=self.with_conv_fullheight,
            final_conv_fullheight=self.with_conv_fullheight,
        )

    def initialize_nets(self, spike_length_samples):
        self.spike_length_samples = spike_length_samples
        self.output_dim = self.wf_dim = (
            spike_length_samples * self.model_channel_index.shape[1]
        )

        if self.exz_estimator in ("n2n", "n3n"):
            self.eyz = self.get_mlp()
        if self.exz_estimator in ("n3n", "2n2", "3n3"):
            self.emz = self.get_mlp()
        if self.inference_kind == "amortized":
            self.inf_net = self.get_mlp()
        self.to(self.relative_index.device)

    def fit(self, waveforms, max_channels):
        waveforms = reindex(max_channels, waveforms, self.relative_index, pad_value=0.0)
        with torch.enable_grad():
            self._fit(waveforms, max_channels)
        self._needs_fit = False

    def forward(self, waveforms, max_channels):
        """Called only at inference time."""
        waveforms = reindex(max_channels, waveforms, self.relative_index, pad_value=0.0)
        masks = self.get_masks(max_channels).to(waveforms)
        net_input = waveforms, masks.unsqueeze(1)

        if self.inference_kind == "amortized":
            pred = self.inf_net(net_input)
        elif self.inference_kind == "raw":
            pred = self.eyz(net_input)
        elif self.inference_kind == "exz":
            if self.exz_estimator == "n2n":
                eyz = self.eyz(net_input)
                pred = 2 * eyz - waveforms
            elif self.exz_estimator == "2n2":
                emz = self.emz(net_input)
                pred = waveforms - 2 * emz
            elif self.exz_estimator == "n3n":
                eyz = self.eyz(net_input)
                emz = self.emz(net_input)
                pred = eyz - emz
            elif self.exz_estimator == "3n3":
                emz = self.emz(net_input)
                pred = waveforms - emz
            else:
                assert False
        elif self.inference_kind == "exy_fake":
            if self.exz_estimator in ("2n2", "3n3"):
                emz = self.emz(net_input)
                pred = waveforms - emz
            else:
                assert False
        else:
            assert False

        pred = reindex(max_channels, pred, self.irrelative_index)

        return pred

    def get_masks(self, max_channels):
        return self.model_channel_index[max_channels] < self.n_channels

    def train_forward(self, y, m, mask):
        z = y + m

        # predictions given z
        exz = eyz = emz = e_exz_y = None
        net_input = z, mask.unsqueeze(1)
        if self.exz_estimator == "n2n":
            eyz = self.eyz(net_input)
            exz = 2 * eyz - z
        elif self.exz_estimator == "2n2":
            emz = self.emz(net_input)
            exz = z - 2 * emz
        elif self.exz_estimator == "n3n":
            eyz = self.eyz(net_input)
            emz = self.emz(net_input)
            exz = eyz - emz
        elif self.exz_estimator == "3n3":
            emz = self.emz(net_input)
            exz = y - emz
        else:
            assert False

        # predictions given y, if relevant
        if self.inference_kind == "amortized":
            e_exz_y = self.inf_net((y, mask.unsqueeze(1)))

        return exz, eyz, emz, e_exz_y

    def loss(self, mask, waveforms, m, exz, eyz=None, emz=None, e_exz_y=None):
        loss_dict = {}
        mask = mask.unsqueeze(1)
        if eyz is not None:
            loss_dict["eyz"] = F.mse_loss(mask * eyz, mask * waveforms)
        if emz is not None:
            loss_dict["emz"] = F.mse_loss(mask * emz, mask * m)
        if e_exz_y is not None:
            loss_dict["e_exz_y"] = F.mse_loss(mask * exz, mask * e_exz_y)
        return loss_dict

    def _fit(self, waveforms, channels):
        self.initialize_nets(waveforms.shape[1])
        waveforms = waveforms.cpu()
        channels = channels.cpu()
        main_dataset = TensorDataset(waveforms, channels)
        noise_dataset = SameChannelNoiseDataset(
            self.recording,
            channels.numpy(force=True),
            self.model_channel_index_np,
            spike_length_samples=self.spike_length_samples,
        )
        dataset = StackDataset(main_dataset, noise_dataset)
        if self.sample_weighting is None:
            sampler = RandomSampler(dataset)
        elif self.sample_weighting == "kmeans":
            main_traces = grab_main_channels(waveforms, channels, self.model_channel_index_np)
            main_traces = main_traces.to(self.relative_index.device)
            densities = kmeanspp_density_estimate(main_traces, rg=0)
            sampler = WeightedRandomSampler(1.0 / densities, num_samples=len(dataset))
        else:
            assert False
        dataloader = DataLoader(
            dataset,
            sampler=BatchSampler(
                sampler,
                batch_size=self.batch_size,
                drop_last=True,
            ),
            num_workers=self.n_data_workers,
            persistent_workers=bool(self.n_data_workers),
        )
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)

        with trange(self.epochs, desc="Epochs", unit="epoch") as pbar:
            for epoch in pbar:
                epoch_losses = {}
                for (waveform_batch, channels_batch), noise_batch in dataloader:
                    # for whatever reason, batch sampler adds an empty dim
                    waveform_batch = waveform_batch[0]
                    channels_batch = channels_batch[0]
                    noise_batch = noise_batch[0]

                    optimizer.zero_grad()

                    # get a batch of noise samples
                    # m = self.get_noise(channels_batch).to(waveform_batch)
                    waveform_batch = waveform_batch.to(self.relative_index.device)
                    m = noise_batch.to(waveform_batch)
                    mask = self.get_masks(channels_batch).to(waveform_batch)
                    exz, eyz, emz, e_exz_y = self.train_forward(waveform_batch, m, mask)
                    loss_dict = self.loss(mask, waveform_batch, m, exz, eyz, emz, e_exz_y)
                    loss = sum(loss_dict.values())
                    loss.backward()
                    optimizer.step()

                    for k, v in loss_dict.items():
                        epoch_losses[k] = v.item() + epoch_losses.get(k, 0.0)

                epoch_losses = {k: v / len(dataloader) for k, v in epoch_losses.items()}
                loss_str = ", ".join(f"{k}: {v:.3f}" for k, v in epoch_losses.items())
                pbar.set_description(f"Epochs [{loss_str}]")


def get_noise(recording, channels, channel_index, spike_length_samples=121, rg=0):
    if rg is not None:
        rg = np.random.default_rng(rg)
        # pick random times
        times_samples = rg.integers(
            recording.get_num_samples() - spike_length_samples,
            size=len(channels),
        )
    else:
        times_samples = torch.randint(
            low=0,
            high=recording.get_num_samples() - spike_length_samples,
            size=(len(channels),),
            device="cpu",
        ).numpy()

    order = np.argsort(times_samples)
    inv_order = np.argsort(order)

    # load waveforms on the same channels and channel neighborhoods
    noise_waveforms = spikeio.read_waveforms_channel_index(
        recording,
        times_samples[order],
        channel_index,
        channels,
        trough_offset_samples=0,
        spike_length_samples=spike_length_samples,
        fill_value=0.0,
    )

    # back to random order
    noise_waveforms = noise_waveforms[inv_order]

    return torch.from_numpy(noise_waveforms)


class SameChannelNoiseDataset(Dataset):
    def __init__(
        self,
        recording,
        channels,
        channel_index,
        spike_length_samples=121,
        with_indices=False,
    ):
        super().__init__()
        self.recording = recording
        self.channels = channels
        self.spike_length_samples = spike_length_samples
        self.channel_index = channel_index
        self.with_indices = with_indices

    def __len__(self):
        return len(self.channels)

    def __getitem__(self, index):
        noise = get_noise(
            self.recording,
            self.channels[index],
            self.channel_index,
            spike_length_samples=self.spike_length_samples,
            rg=None,
        )
        if self.with_indices:
            return index, noise
        return noise


def kmeanspp_density_estimate(x, n_components=1024, n_iter=10, sigma=10.0, learn_sigma=True, rg=0):
    rg = np.random.default_rng(0)

    # kmeanspp
    n = len(x)
    centroid_ixs = []
    dists = torch.full(
        (n,), torch.inf, dtype=x.dtype, device=x.device
    )
    assignments = torch.zeros((n,), dtype=torch.long, device=x.device)
    for j in range(n_components):
        if j == 0:
            newix = rg.integers(n)
        else:
            p = torch.nan_to_num(dists)
            newix = rg.choice(n, p=(p / p.sum()).numpy(force=True))

        centroid_ixs.append(newix)
        curcent = x[newix][None]
        newdists = (x - curcent).square_().sum(1)
        closer = newdists < dists
        assignments[closer] = j
        dists[closer] = newdists[closer]

    # soft lloyd
    e = None
    if n_iter:
        centroids = x[centroid_ixs]
        dists = torch.cdist(x, centroids).square_()
        for i in range(n_iter):
            # update responsibilities, n x k
            e = F.softmax(-0.5 * dists / (sigma ** 2), dim=1)

            # normalize per centroid
            e = e.div_(e.sum(0))

            # update centroids
            centroids = e.T @ x
            dists = torch.cdist(x, centroids).square_()
            assignments = torch.argmin(dists, 1)
            if learn_sigma:
                sigma = torch.take_along_dim(dists, assignments[:, None], dim=1).mean().sqrt()
            if e.shape[1] == 1:
                break

    # estimate densities
    dists = torch.cdist(x, centroids).square_()
    e = F.softmax(-0.5 * dists / (sigma ** 2), dim=1)
    component_proportion = e.mean(0)
    density = e @ component_proportion

    return density