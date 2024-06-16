import numpy as np
from itertools import product
import bigstream.utility as ut
import os, tempfile
from ClusterWrap.decorator import cluster
import dask.array as da
import zarr
import bigstream.transform as bs_transform
from dask.distributed import as_completed


@cluster
def distributed_apply_transform(
    fix_zarr, mov_zarr,
    fix_spacing, mov_spacing,
    transform_list,
    blocksize,
    write_path=None,
    overlap=0.5,
    dataset_path=None,
    temporary_directory=None,
    cluster=None,
    cluster_kwargs={},
    **kwargs,
):
    """
    Resample a larger-than-memory moving image onto a fixed image through a
    list of transforms

    Parameters
    ----------
    fix : zarr array
        The fixed image data

    mov : zarr array
        The moving image data

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image. Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image. Length must equal `mov.ndim`

    transform_list : list
        The list of transforms to apply. These may be 2d arrays of shape 4x4
        (affine transforms), or ndarrays of `fix.ndim` + 1 dimensions (deformations).
        Zarr arrays work just fine.

    blocksize : iterable
        The shape of blocks in voxels

    write_path : string (default: None)
        Location on disk to write the resampled data as a zarr array

    overlap : float in range [0, 1] (default: 0.5)
        Block overlap size as a percentage of block size

    dataset_path : string (default: None)
        A subpath in the zarr array to write the resampled data to

    temporary_directory : string (default: None)
        A parent directory for temporary data written to disk during computation
        If None then the current directory is used

    cluster : ClusterWrap.cluster object (default: None)
        Only set if you have constructed your own static cluster. The default behavior
        is to construct a cluster for the duration of this function, then close it
        when the function is finished.

    cluster_kwargs : dict (default: {})
        Arguments passed to ClusterWrap.cluster
        If working with an LSF cluster, this will be
        ClusterWrap.janelia_lsf_cluster. If on a workstation
        this will be ClusterWrap.local_cluster.
        This is how distribution parameters are specified.

    **kwargs : Any additional keyword arguments
        Passed to bigstream.transform.apply_transform

    Returns
    -------
    resampled : array
        The resampled moving data with transform_list applied. If write_path is not None
        this will be a zarr array. Otherwise it is a numpy array.
    """

    # temporary file paths and ensure inputs are zarr
    temporary_directory = tempfile.TemporaryDirectory(
        prefix='.', dir=temporary_directory or os.getcwd(),
    )
    fix_zarr_path = temporary_directory.name + '/fix.zarr'
    mov_zarr_path = temporary_directory.name + '/mov.zarr'
    zarr_blocks = (128,)*fix_zarr.ndim
    fix_zarr = ut.numpy_to_zarr(fix_zarr, zarr_blocks, fix_zarr_path)
    mov_zarr = ut.numpy_to_zarr(mov_zarr, zarr_blocks, mov_zarr_path)

    # ensure all deforms are zarr
    new_list = []
    zarr_blocks = (128,)*3 + (3,)
    for iii, transform in enumerate(transform_list):
        if transform.shape != (4, 4):
            zarr_path = temporary_directory.name + f'/deform{iii}.zarr'
            transform = ut.numpy_to_zarr(transform, zarr_blocks, zarr_path)
        new_list.append(transform)
    transform_list = new_list

    # ensure transform spacing is set explicitly
    if 'transform_spacing' not in kwargs.keys():
        kwargs['transform_spacing'] = np.array(fix_spacing)
    if not isinstance(kwargs['transform_spacing'], tuple):
        kwargs['transform_spacing'] = (kwargs['transform_spacing'],) * len(transform_list)

    # get overlap and number of blocks
    blocksize = np.array(blocksize)
    overlap = np.round(blocksize * overlap).astype(int)  # NOTE: default overlap too big?
    nblocks = np.ceil(np.array(fix_zarr.shape) / blocksize).astype(int)

    # store block coordinates in a dask array
    # TODO: remove use of dask array
    block_coords = np.empty(nblocks, dtype=tuple)
    for (i, j, k) in np.ndindex(*nblocks):
        start = blocksize * (i, j, k) - overlap
        stop = start + blocksize + 2 * overlap
        start = np.maximum(0, start)
        stop = np.minimum(fix_zarr.shape, stop)
        block_coords[i, j, k] = tuple(slice(x, y) for x, y in zip(start, stop))
    block_coords = da.from_array(block_coords, chunks=(1,)*block_coords.ndim)

    # pipeline to run on each block
    def transform_single_block(coords, transform_list):

        # fetch fixed image slices and read fix
        fix_slices = coords.item()
        fix = fix_zarr[fix_slices]
        fix_origin = fix_spacing * [s.start for s in fix_slices]
        print('Block coords:', fix_slices,
            'Block origin:', fix_origin,
            'Block size:', blocksize,
            'Overlap:', overlap,
            flush=True)

        # read relevant region of transforms
        new_list = []
        transform_origin = [fix_origin,] * len(transform_list)
        for iii, transform in enumerate(transform_list):
            if transform.shape != (4, 4):
                start = np.floor(fix_origin / kwargs['transform_spacing'][iii]).astype(int)
                stop = [s.stop for s in fix_slices] * fix_spacing / kwargs['transform_spacing'][iii]
                stop = np.ceil(stop).astype(int)
                transform_slice = tuple(slice(a, b) for a, b in zip(start, stop))
                print(f'Transform slice for block at {fix_slices}:',
                    transform_slice,
                    flush=True)
                transform = transform[transform_slice]
                transform_origin[iii] = start * kwargs['transform_spacing'][iii]

            if fix_slices[0].start == 0 and fix_slices[1].start == 96 and fix_slices[2].start == 96:
                print(f'!!!!!! transform {iii}: ',
                      transform_origin[iii],
                      transform.shape,
                      transform,
                      flush=True)
            new_list.append(transform)
        transform_list = new_list
        transform_origin = tuple(transform_origin)
        print(f'Transform origin for block at {fix_slices}:',
            transform_origin,
            flush=True)

        # transform fixed block corners, read moving data
        fix_block_coords = []
        for corner in list(product([0, 1], repeat=3)):
            a = [x.stop-1 if y else x.start for x, y in zip(fix_slices, corner)]
            fix_block_coords.append(a)
        fix_block_coords = np.array(fix_block_coords) * fix_spacing
        mov_block_coords = bs_transform.apply_transform_to_coordinates(
            fix_block_coords, transform_list, kwargs['transform_spacing'], transform_origin,
        )
        print('Transformed moving block coords:', fix_slices, 
            fix_block_coords, '->', mov_block_coords,
            flush=True)
        mov_block_coords = np.round(mov_block_coords / mov_spacing).astype(int)
        mov_block_coords = np.maximum(0, mov_block_coords)
        mov_block_coords = np.minimum(mov_zarr.shape, mov_block_coords)
        mov_start = np.min(mov_block_coords, axis=0)
        mov_stop = np.max(mov_block_coords, axis=0)
        mov_slices = tuple(slice(a, b) for a, b in zip(mov_start, mov_stop))
        mov = mov_zarr[mov_slices]
        mov_origin = mov_spacing * [s.start for s in mov_slices]
        if fix_slices[0].start == 0 and fix_slices[1].start == 96 and fix_slices[2].start == 96:
            print('!!!!!! MOV: ', mov_block_coords,
                  mov_slices, mov_origin, mov,
                  flush=True)

        print(f'Moving block {fix_slices} origin:', fix_origin, '->',
              mov_origin, flush=True)
        print(f'Moving block {fix_slices} coords: -> {mov_slices}',
            flush=True)

        # resample
        print(f'Apply {len(transform_list)} transforms to {fix_slices}',
              f'fix origin: {fix_origin}, mov origin: {mov_origin}',
              flush=True)
        aligned = bs_transform.apply_transform(
            fix, mov, fix_spacing, mov_spacing,
            transform_list=transform_list,
            transform_origin=transform_origin,
            fix_origin=fix_origin,
            mov_origin=mov_origin,
            **kwargs,
        )
        if fix_slices[0].start == 0 and fix_slices[1].start == 96 and fix_slices[2].start == 96:
            print('!!!!!! ALIGNED: ', fix_origin,
                  mov_origin, transform_origin, 
                  aligned.shape, aligned,
                  flush=True)

        # crop out overlap
        for axis in range(aligned.ndim):

            # left side
            slc = [slice(None),]*aligned.ndim
            if fix_slices[axis].start != 0:
                slc[axis] = slice(overlap[axis], None)
                print('Crop axis', axis, 'left', 
                    fix_slices,'->',slc,
                    flush=True)
                aligned = aligned[tuple(slc)]

            # right side
            slc = [slice(None),]*aligned.ndim
            if aligned.shape[axis] > blocksize[axis]:
                print('Crop axis', axis, 'right', fix_slices,'->',slc,
                    flush=True)
                slc[axis] = slice(None, blocksize[axis])
                aligned = aligned[tuple(slc)]

        # return result
        return aligned
    # END: closure

    # align all blocks
    aligned = da.map_blocks(
        transform_single_block,
        block_coords,
        transform_list=transform_list,
        dtype=fix_zarr.dtype,
        chunks=blocksize,
    )

    # crop to original size
    aligned = aligned[tuple(slice(s) for s in fix_zarr.shape)]

    # return
    if write_path:
        da.to_zarr(aligned, write_path, component=dataset_path)
        return zarr.open(write_path, 'r+')
    else:
        return aligned.compute()


@cluster
def distributed_apply_transform_to_coordinates(
    coordinates,
    transform_list,
    partition_size=30.,
    transform_spacing=None,
    transform_origin=None,
    temporary_directory=None,
    cluster=None,
    cluster_kwargs={},
):
    """
    Move a set of coordinates through a list of transforms
    Transforms can be larger-than-memory

    Parameters
    ----------
    coordinates : Nxd array
        The coordinates to move. N such coordinates in d dimensions.

    transform_list : list
        The transforms to apply, in stack order. Elements must be 2d 4x4 arrays
        (affine transforms) or d + 1 dimension arrays (deformations).
        Zarr arrays work just fine.

    partition_size : scalar float (default: 30.)
        Size of blocks that domain is carved into for distributed computation
        in same units as coordinates

    transform_spacing : 1d array or tuple of 1d arrays (default: None)
        The spacing in physical units (e.g. mm or um) between voxels
        of any deformations in the transform_list. If any transform_list
        contains any deformations then transform_spacing cannot be None.
        If a single 1d array then all deforms have that spacing.
        If a tuple, then its length must be the same as transform_list,
        thus each deformation can be given its own spacing. Spacings given
        for affine transforms are ignored.

    transform_origin : 1d array or tuple of 1d arrays (default: None)
        The origin in physical units (e.g. mm or um) of the given transforms.
        If None, all origins are assumed to be (0, 0, 0, ...); otherwise, follows
        the same logic as transform_spacing. Origins given for affine transforms
        are ignored.

    temporary_directory : string (default: None)
        A parent directory for temporary data written to disk during computation
        If None then the current directory is used

    cluster : ClusterWrap.cluster object (default: None)
        Only set if you have constructed your own static cluster. The default behavior
        is to construct a cluster for the duration of this function, then close it
        when the function is finished.

    cluster_kwargs : dict (default: {})
        Arguments passed to ClusterWrap.cluster
        If working with an LSF cluster, this will be
        ClusterWrap.janelia_lsf_cluster. If on a workstation
        this will be ClusterWrap.local_cluster.
        This is how distribution parameters are specified.

    Returns
    -------
    transformed_coordinates : Nxd array
        The given coordinates transformed by the given transform_list
    """

    # TODO: check this for multiple deforms and transform_origin as a list

    # ensure temporary directory exists
    temporary_directory = temporary_directory or os.getcwd()
    temporary_directory = tempfile.TemporaryDirectory(
        prefix='.', dir=temporary_directory,
    )

    # ensure all deforms are zarr
    new_list = []
    zarr_blocks = (128,)*3 + (3,)
    for iii, transform in enumerate(transform_list):
        if not len(transform.shape) in [1, 2]:
            zarr_path = temporary_directory.name + f'/deform{iii}.zarr'
            transform = ut.numpy_to_zarr(transform, zarr_blocks, zarr_path)
        new_list.append(transform)
    transform_list = new_list

    # determine partitions of coordinates
    origin = np.min(coordinates, axis=0)
    nblocks = np.max(coordinates, axis=0) - origin
    nblocks = np.ceil(nblocks / partition_size).astype(int)
    partitions, indices = [], []
    for index in np.ndindex(*nblocks):
        lower_bound = origin + partition_size * np.array(index)
        upper_bound = lower_bound + partition_size
        not_too_low = np.all(coordinates >= lower_bound, axis=1)
        not_too_high = np.all(coordinates < upper_bound, axis=1)
        part_indices = np.nonzero( not_too_low * not_too_high )[0]
        if part_indices.size != 0:
            partitions.append(coordinates[part_indices])
            indices.append(part_indices)
    indices = np.concatenate(indices, axis=0)

    def transform_partition(coordinates, transform_list):

        # read relevant region of transform
        a = np.min(coordinates, axis=0)
        b = np.max(coordinates, axis=0)
        new_list = []
        for iii, transform in enumerate(transform_list):
            if not len(transform.shape) in [1, 2]:
                spacing = transform_spacing
                if isinstance(spacing, tuple): spacing = spacing[iii]
                start = np.floor(a / spacing).astype(int)
                last_index = np.array(transform.shape[:-1]) - 1
                start = np.minimum(last_index, start)
                stop = np.ceil(b / spacing).astype(int) + 1
                crop = tuple(slice(x, y) for x, y in zip(start, stop))
                transform = transform[crop]
            new_list.append(transform)
        transform_list = new_list

        # apply transforms
        return bs_transform.apply_transform_to_coordinates(
            coordinates, transform_list,
            transform_spacing,
            transform_origin=a,
        )

    # transform all partitions and return
    futures = cluster.client.map(
        transform_partition, partitions,
        transform_list=transform_list,
    )
    permuted = np.concatenate(cluster.client.gather(futures), axis=0)
    results = np.empty_like(permuted)
    results[indices] = permuted
    return results


@cluster
def distributed_invert_displacement_vector_field(
    field,
    spacing,
    blocksize,
    write_path=None,
    step=0.5,
    iterations=10,
    sqrt_order=2,
    sqrt_step=0.5,
    sqrt_iterations=5,
    cluster=None,
    cluster_kwargs={},
    temporary_directory=None,
):
    """
    Numerically find the inverse of a larger-than-memory displacement vector field

    Parameters
    ----------
    field : array like, can be zarr or numpy array
        The displacement vector field to invert

    spacing : 1d-array
        The physical voxel spacing of the displacement field

    blocksize : iterable
        The shape of blocks in voxels

    write_path : string (default: None)
        Location on disk to write the inverted displacement field
        If None, then the inverted transform is returned in memory
        to the client process (make sure you have enough RAM if you do this!)

    step : float (default: 0.5)
        The step size used for each iteration of the stationary point algorithm

    iterations : scalar int (default: 10)
        The number of stationary point iterations to find inverse. More
        iterations gives a more accurate inverse but takes more time.

    sqrt_order : scalar int (default: 2)
        The number of roots to take before stationary point iterations.

    sqrt_step : float (default: 0.5)
        The step size used for each iteration of the composition square root gradient descent

    sqrt_iterations : scalar int (default: 5)
        The number of iterations to find the field composition square root.

    cluster : ClusterWrap.cluster object (default: None)
        Only set if you have constructed your own static cluster. The default behavior
        is to construct a cluster for the duration of this function, then close it
        when the function is finished.

    cluster_kwargs : dict (default: {})
        Arguments passed to ClusterWrap.cluster
        If working with an LSF cluster, this will be
        ClusterWrap.janelia_lsf_cluster. If on a workstation
        this will be ClusterWrap.local_cluster.
        This is how distribution parameters are specified.

    temporary_directory : string (default: None)
        Temporary files may be created during inversion. The temporary files will be
        in their own folder within the `temporary_directory`. The default is the
        current directory. Temporary files are removed if the function completes
        successfully.

    Returns
    -------
    inverse_field : zarr array
        The numerical inverse of the given displacement vector field as a zarr array.
    """

    # ensure input field is zarr
    temporary_directory = tempfile.TemporaryDirectory(
        prefix='.', dir=temporary_directory or os.getcwd(),
    )
    zarr_blocks = tuple(blocksize) + (field.shape[-1],)
    field_zarr_path = temporary_directory.name + '/field.zarr'
    field_zarr = ut.numpy_to_zarr(field, zarr_blocks, field_zarr_path)

    # create output array
    if write_path:
        output_zarr = ut.create_zarr(
            write_path,
            field_zarr.shape,
            zarr_blocks,
            field_zarr.dtype,
        )

    # get overlap and number of blocks
    blocksize = np.array(blocksize)
    overlap = np.round(blocksize * 0.25).astype(int)
    nblocks = np.ceil(np.array(field_zarr.shape[:-1]) / blocksize).astype(int)

    # determine block coordinates
    block_coords = []
    block_coords_overlaps = []
    for (i, j, k) in np.ndindex(*nblocks):
        start = blocksize * (i, j, k)
        stop = start + blocksize
        start_ol = start - overlap
        stop_ol = stop + overlap

        start_ol = np.maximum(0, start_ol)
        stop = np.minimum(field_zarr.shape[:-1], stop)
        stop_ol = np.minimum(field_zarr.shape[:-1], stop_ol)

        coords = tuple(slice(x, y) for x, y in zip(start, stop))
        coords_ol = tuple(slice(x, y) for x, y in zip(start_ol, stop_ol))
        block_coords.append(coords)
        block_coords_overlaps.append(coords_ol)

    # the function to run on every block
    def invert_block(slices, slices_overlaps):

        field = field_zarr[slices_overlaps]
        inverse = bs_transform.invert_displacement_vector_field(
            field,
            spacing,
            step=step,
            iterations=iterations,
            sqrt_order=sqrt_order,
            sqrt_step=sqrt_step,
            sqrt_iterations=sqrt_iterations,
        )

        # crop out overlap
        for axis in range(inverse.ndim - 1):

            # left side
            slc = [slice(None),]*(inverse.ndim - 1)
            if slices_overlaps[axis].start != 0:
                slc[axis] = slice(overlap[axis], None)
                inverse = inverse[tuple(slc)]

            # right side
            slc = [slice(None),]*(inverse.ndim - 1)
            if inverse.shape[axis] > blocksize[axis]:
                slc[axis] = slice(None, blocksize[axis])
                inverse = inverse[tuple(slc)]

        # handle output
        if not write_path:
            return inverse
        else:
            output_zarr[slices] = inverse
            return True

    # submit all blocks
    futures = cluster.client.map(
        invert_block,
        block_coords,
        block_coords_overlaps,
    )

    # reconstruct output if necessary
    if not write_path:
        future_keys = [f.key for f in futures]
        inverse = np.zeros_like(field)
        for batch in as_completed(futures, with_results=True).batches():
            for future, result in batch:
                iii = future_keys.index(future.key)
                inverse[block_coords[iii]] = result
        return inverse
    else:
        all_written = np.all(cluster.client.gather(futures))
        return output_zarr

