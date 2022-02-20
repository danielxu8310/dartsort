import numpy as np

from sklearn.decomposition import PCA
from tqdm.auto import trange
from joblib import Parallel, delayed

from .waveform_utils import relativize_waveforms  # noqa
from .point_source_centering import relocate_simple


def pca_reload(
    original_waveforms,
    relocated_waveforms,
    orig_ptps,
    standard_ptps,
    rank=10,
    B_updates=2,
    pbar=True,
):
    N, T, C = original_waveforms.shape
    assert relocated_waveforms.shape == (N, T, C)
    assert orig_ptps.shape == standard_ptps.shape == (N, C)
    xrange = trange if pbar else range

    destandardization = (orig_ptps / standard_ptps)[:, None, :]

    # fit PCA in relocated space
    reloc_pca = PCA(rank).fit(relocated_waveforms.reshape(N, T * C))
    pca_basis = reloc_pca.components_.reshape(rank, T, C)

    # rank 0 model
    relocated_mean = reloc_pca.mean_.reshape(T, C)
    unrelocated_means = relocated_mean[None, :, :] * destandardization
    decentered_original_waveforms = original_waveforms - unrelocated_means

    # re-compute the loadings to minimize loss in original space
    reloadings = np.zeros((N, rank))
    err = 0.0
    for n in xrange(N):
        A = (
            (destandardization[n, None, :, :] * pca_basis)
            .reshape(rank, T * C)
            .T
        )
        b = decentered_original_waveforms[n].reshape(T * C)
        x, resid, *_ = np.linalg.lstsq(A, b, rcond=None)
        reloadings[n] = x
        err += resid
    err = err / (N * T * C)

    for _ in xrange(B_updates, desc="B updates"):
        # update B
        # flat view
        B = pca_basis.reshape(rank, T * C)
        W = decentered_original_waveforms.reshape(N, T * C)
        for c in range(C):
            A = destandardization[:, 0, c, None] * reloadings
            for t in xrange(T):
                i = t * C + c
                res, *_ = np.linalg.lstsq(A, W[:, i], rcond=None)
                B[:, i] = res

        # re-update reloadings
        reloadings = np.zeros((N, rank))
        err = 0.0
        for n in xrange(N):
            A = (
                (destandardization[n, None, :, :] * pca_basis)
                .reshape(rank, T * C)
                .T
            )
            b = decentered_original_waveforms[n].reshape(T * C)
            x, resid, *_ = np.linalg.lstsq(A, b, rcond=None)
            reloadings[n] = x
            err += resid
        err = err / (N * T * C)

    return reloadings, err


def relocated_ae(
    waveforms,
    firstchans,
    maxchans,
    geom,
    x,
    y,
    z_rel,
    alpha,
    relocate_dims="xyza",
    rank=10,
    B_updates=2,
    pbar=True,
):
    # -- compute the relocation
    waveforms_reloc, std_ptp, pred_ptp = relocate_simple(
        waveforms,
        geom,
        firstchans,
        maxchans,
        x,
        y,
        z_rel,
        alpha,
        relocate_dims=relocate_dims,
    )
    # torch -> numpy
    waveforms_reloc = waveforms_reloc.cpu().numpy()
    std_ptp = std_ptp.cpu().numpy()
    pred_ptp = pred_ptp.cpu().numpy()

    # -- get the features
    feats, err = pca_reload(
        waveforms,
        waveforms_reloc,
        pred_ptp,
        std_ptp,
        rank=rank,
        B_updates=B_updates,
        pbar=pbar,
    )

    return feats, err


def relocated_ae_batched(
    waveforms,
    firstchans,
    maxchans,
    geom,
    x,
    y,
    z_rel,
    alpha,
    relocate_dims="xyza",
    rank=10,
    B_updates=2,
    batch_size=50000,
    n_jobs=1,
):
    N, T, C = waveforms.shape

    # we should be able to store features in memory:
    # 5 million spikes x 10 features x 4 bytes is like .2 gig
    features = np.empty((N, rank))
    errors = np.empty(N // batch_size + 1)

    @delayed
    def job(bs):
        be = min(bs + batch_size, N)
        feats, err = relocated_ae(
            waveforms[bs:be],
            firstchans[bs:be],
            maxchans[bs:be],
            geom,
            x[bs:be],
            y[bs:be],
            z_rel[bs:be],
            alpha[bs:be],
            relocate_dims="xyza",
            rank=rank,
            B_updates=B_updates,
            pbar=False,
        )
        return bs, be, feats, err

    i = 0
    for bs, be, feats, err in Parallel(n_jobs)(
        job(bs) for bs in trange(0, N, batch_size, desc="Feature batches")
    ):
        features[bs:be] = feats
        errors[i] = err
        i += 1

    return features, errors
