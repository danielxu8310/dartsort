import numpy as np
import h5py
import torch
from torch import nn
from tqdm.auto import tqdm, trange

from . import waveform_utils


class SingleChanDenoiser(nn.Module):
    """Cleaned up a little. Why is conv3 here and commented out in forward?"""

    def __init__(
        self, n_filters=[16, 8, 4], filter_sizes=[5, 11, 21], spike_size=121
    ):
        super(SingleChanDenoiser, self).__init__()
        feat1, feat2, feat3 = n_filters
        size1, size2, size3 = filter_sizes
        self.conv1 = nn.Sequential(nn.Conv1d(1, feat1, size1), nn.ReLU())
        self.conv2 = nn.Sequential(nn.Conv1d(feat1, feat2, size2), nn.ReLU())
        self.conv3 = nn.Sequential(nn.Conv1d(feat2, feat3, size3), nn.ReLU())
        n_input_feat = feat2 * (spike_size - size1 - size2 + 2)
        self.out = nn.Linear(n_input_feat, spike_size)

    def forward(self, x):
        x = x[:, None]
        x = self.conv1(x)
        x = self.conv2(x)
        # x = self.conv3(x)
        x = x.view(x.shape[0], -1)
        return self.out(x)

    def load(self, fname_model):
        checkpoint = torch.load(fname_model, map_location="cpu")
        self.load_state_dict(checkpoint)


def spike_train_to_index(spike_train, templates):
    """Convert a kilosort spike train to a spike index

    KS spike train contains (sample, id) pairs. Spike index
    contains (sample, max channel) pairs.

    Output times are min PTP times, output max chans are KS
    template max chans.
    """
    n_templates = templates.shape[0]
    template_ptps = templates.ptp(1)
    template_maxchans = template_ptps.argmax(1)

    cluster_ids = spike_train[:, 1]
    template_offsets = templates[np.arange(n_templates), :, template_maxchans].argmin(1)
    spike_offsets = template_offsets[cluster_ids] - 42
    start_times = spike_train[:, 0] + spike_offsets

    spike_index = np.c_[start_times, template_maxchans[cluster_ids]]
    return spike_index


def get_denoised_waveforms(
    standardized_bin,
    spike_index,
    geom,
    channel_radius=10,
    denoiser_weights_path="../pretrained/single_chan_denoiser.pt",
    T=121,
    threshold=6.0,
    dtype=np.float32,
    geomkind="updown",
    batch_size=128,
    device=None,
):
    num_channels = geom.shape[0]
    standardized = np.memmap(standardized_bin, dtype=dtype, mode="r")
    standardized = standardized.reshape(-1, num_channels)

    # load denoiser
    denoiser = SingleChanDenoiser()
    denoiser.load(denoiser_weights_path)
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(device)
    denoiser.to(device)

    # spike times are min PTP times, which need to be centered in our output
    # also, make sure we don't read past the edge of the file
    read_times = spike_index[:, 0] - T // 2
    good = np.flatnonzero(
        (read_times >= 0) & (read_times + T < standardized.shape[0])
    )
    read_times = read_times[good]
    spike_index = spike_index[good]

    # helper function for data loading
    def get_batch(start, end):
        times, maxchans = spike_index[start:end].T
        waveforms = np.array(
            [standardized[s:t] for s, t in zip(times, times[1:])]
        )
        waveforms_trimmed = waveform_utils.get_local_waveforms(
            waveforms,
            channel_radius,
            geom,
            maxchans=maxchans,
            geomkind=geomkind,
        )
        maxptps = waveforms_trimmed.ptp(1).max(1)
        return waveforms_trimmed[maxptps > threshold]

    # we probably won't find this many spikes that cross the threshold,
    # but we can use it to allocate storage
    max_n_spikes = len(spike_index)
    denoised_waveforms = np.empty(
        (max_n_spikes, T, 2 * channel_radius + 2 * (geomkind == "standard")),
        dtype=dtype,
    )
    count = 0  # how many spikes have exceeded the threshold?
    for i in trange(max_n_spikes // batch_size + 1):
        start = i * batch_size
        end = min(max_n_spikes, (i + 1) * batch_size)
        batch = torch.as_tensor(get_batch(start, end), device=device)
        n_batch = batch.shape[0]
        denoised_batch = denoiser(batch)
        denoised_waveforms[count : count + n_batch] = denoised_batch
        count += n_batch
    # trim the places we did not fill
    denoised_waveforms = denoised_waveforms[:count]

    return denoised_waveforms