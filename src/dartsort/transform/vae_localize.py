import torch
import torch.nn as nn
import torch.nn.functional as F
from dartsort.util.spiketorch import get_relative_index, ptp, reindex
from dartsort.util.waveform_util import make_regular_channel_index
from torch.utils.data import DataLoader, TensorDataset
from tqdm.auto import trange

from .transform_base import BaseWaveformFeaturizer


class VAELocalization(BaseWaveformFeaturizer):
    """Order of output columns: x, y, z_abs, alpha"""

    default_name = "vae_point_source_localizations"
    shape = (4,)
    dtype = torch.double

    def __init__(
        self,
        channel_index,
        geom,
        radius=100.0,
        amplitude_kind="peak",
        localization_model="pointsource",
        hidden_dims=(256, 128),
        name=None,
        name_prefix="",
        epochs=10,
        learning_rate=1e-3,
        batch_size=32,
        alpha_closed_form=False,
    ):
        assert amplitude_kind in ("peak", "ptp")
        super().__init__(
            geom=geom,
            channel_index=channel_index,
            name=name,
            name_prefix=name_prefix,
        )
        self.amplitude_kind = amplitude_kind
        self.radius = radius
        self.localization_model = localization_model
        self.latent_dim = 3 + (not alpha_closed_form)
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.encoder = None
        self.register_buffer("padded_geom", F.pad(self.geom.to(torch.float), (0, 0, 0, 1)))
        self.hidden_dims = hidden_dims
        self.alpha_closed_form = alpha_closed_form

        self.register_buffer(
            "model_channel_index",
            make_regular_channel_index(geom=self.geom, radius=radius, to_torch=True),
        )
        self.register_buffer(
            "relative_index",
            get_relative_index(self.channel_index, self.model_channel_index),
        )
    
    def initialize_net(self, spike_length_samples):
        if self.encoder is not None:
            return

        self.encoder = nn.Sequential(
            nn.Linear((spike_length_samples + 1) * self.model_channel_index.shape[1], self.hidden_dims[0]),
            nn.BatchNorm1d(self.hidden_dims[0]),
            nn.ReLU(),
            nn.Linear(self.hidden_dims[0], self.hidden_dims[1]),
            nn.BatchNorm1d(self.hidden_dims[1]),
            nn.ReLU(),
            nn.Linear(self.hidden_dims[1], self.latent_dim * 2),  # Output mu and var
        )
        self.encoder.to(self.padded_geom.device)

    def reparameterize(self, mu, var):
        std = var.sqrt()
        eps = torch.randn_like(std)
        return mu + eps * std

    def local_distances(self, z, channels):
        """Return distances from each z to its local geom centered at channels."""
        centers = self.padded_geom[channels]
        neighbors = self.padded_geom[self.model_channel_index[channels]]
        local_geom = neighbors - centers.unsqueeze(1)
        dx = z[:, 0, None] - local_geom[:, :, 0]
        dz = z[:, 2, None] + local_geom[:, :, 1]
        y = F.softplus(z[:, 1]).unsqueeze(1)
        dists = torch.sqrt(dx**2 + dz**2 + y**2)
        return dists

    def get_alphas(self, obs_amps, dists, masks, return_pred=False):
        pred_amps_alpha1 = 1.0 / dists
        # least squares with no intercept
        alphas = (masks * obs_amps * pred_amps_alpha1).sum(1) / (masks * pred_amps_alpha1).square().sum(1)
        if return_pred:
            return alphas, alphas.unsqueeze(1) * pred_amps_alpha1
        return alphas

    def decode(self, z, channels, obs_amps, masks):
        dists = self.local_distances(z, channels)
        if self.alpha_closed_form:
            alphas, pred_amps = self.get_alphas(obs_amps, dists, masks, return_pred=True)
        else:
            alphas = F.softplus(z[:, 3])
            pred_amps = alphas.unsqueeze(1) / dists
        return alphas, pred_amps

    def forward(self, x, mask, obs_amps, channels):
        x_flat = x.view(x.size(0), -1)
        x_flat_mask = torch.cat((x_flat, mask), dim=1)
        mu, var = self.encoder(x_flat_mask).chunk(2, dim=-1)
        var = F.softplus(var)
        z = self.reparameterize(mu, var)
        alphas, pred_amps = self.decode(z, channels, obs_amps, mask)
        return pred_amps, mu, var

    def loss_function(self, recon_x, x, mask, mu, var):
        recon_x_masked = recon_x * mask
        x_masked = x * mask
        MSE = F.mse_loss(recon_x_masked, x_masked, reduction="sum")
        KLD = -0.5 * (1 + torch.log(var) - mu.pow(2) - var).sum()
        return MSE, KLD

    def _fit(self, waveforms, channels):
        # apply channel reindexing before any fitting...
        waveforms = reindex(channels, waveforms, self.relative_index, pad_value=0.0)
        # should be no nans there thanks to padding with 0s

        if self.amplitude_kind == "ptp":
            amps = ptp(waveforms)
        elif self.amplitude_kind == "peak":
            amps = waveforms.abs().max(dim=1).values
            
        self.initialize_net(waveforms.shape[1])

        dataset = TensorDataset(waveforms, amps, channels)
        dataloader = DataLoader(dataset, batch_size=self.batch_size, shuffle=True)
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)

        self.train()
        with trange(self.epochs, desc="Epochs", unit="epoch") as pbar:
            for epoch in pbar:
                total_loss = 0
                total_bce = 0
                total_kld = 0
                for waveform_batch, amps_batch, channels_batch in dataloader:
                    optimizer.zero_grad()
                    channels_mask = self.model_channel_index[channels_batch] < len(self.geom)
                    channels_mask = channels_mask.to(waveform_batch)
                    reconstructed_amps, mu, var = self.forward(
                        waveform_batch, channels_mask, amps_batch, channels_batch
                    )
                    mse, kld = self.loss_function(
                        reconstructed_amps, amps_batch, channels_mask, mu, var
                    )
                    loss = bce + kld
                    loss.backward()
                    optimizer.step()
                    total_loss += loss.item()
                    total_mse += mse.item()
                    total_kld += kld.item()

                pbar.set_description(f"Epochs [loss={total_loss / len(dataloader):0.2f},mse={total_mse/len(dataloader):0.2f},kld={total_kld/len(dataloader):0.2f}]")

    def fit(self, waveforms, max_channels):
        with torch.enable_grad():
            self._fit(waveforms, max_channels)

    def transform(self, waveforms, max_channels, return_amps=False):
        """
        waveforms : torch.tensor, shape (num_waveforms, n_timesteps, n_channels_subset)
        max_channels : torch.tensor, shape (num_waveforms,)

        waveform[n] lives on channels self.channel_index[max_channels[n]]
        """
        waveforms = reindex(max_channels, waveforms, self.relative_index, pad_value=0.0)
        mask = self.model_channel_index[max_channels] < len(self.geom)
        mask = mask.to(waveforms)
        x_flat = waveforms.view(len(waveforms), -1)
        x_flat_mask = torch.cat((x_flat, mask), dim=1)
        mu, var = self.encoder(x_flat_mask).chunk(2, dim=-1)
        x, y, z = mu[:, :3].T
        y = F.softplus(y)
        mx, mz = self.geom[max_channels].T
        if return_amps:
            if self.amplitude_kind == "ptp":
                obs_amps = ptp(waveforms)
            elif self.amplitude_kind == "peak":
                obs_amps = waveforms.abs().max(dim=1).values
            alphas, pred_amps = self.decode(mu, max_channels, obs_amps, mask)
            return x + mx, y, z + mz, obs_amps, pred_amps, mx, mz
            
        return x + mx, y, z + mz