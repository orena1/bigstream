import numpy as np
import time

import bigstream.transform as cs_transform
import bigstream.utility as ut

from itertools import product
from ClusterWrap.decorator import cluster
from dask.distributed import as_completed


@cluster
def distributed_apply_transform(
    fix, mov,
    fix_spacing, mov_spacing,
    blocksize,
    transform_list,
    overlap_factor=0.5,
    aligned_data=None,
    transform_spacing=None,
    cluster=None,
    cluster_kwargs={},
    **kwargs,
):
    """
    Resample a larger-than-memory moving image onto a fixed image through a
    list of transforms

    Parameters
    ----------
    fix_zarr : zarr array
        The fixed image data

    mov_zarr : zarr array
        The moving image data

    fix_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the fixed image. Length must equal `fix.ndim`

    mov_spacing : 1d array
        The spacing in physical units (e.g. mm or um) between voxels
        of the moving image. Length must equal `mov.ndim`

    blocksize : tuple
        The block partition size used for distributing the work

    transform_list : list
        The list of transforms to apply. These may be 2d arrays of shape 4x4
        (affine transforms), or ndarrays of `fix.ndim` + 1 dimensions (deformations).
        Zarr arrays work just fine.
        If they are ndarrays there's no need to save these as temporary zarr since they
        already come either from a zarr or N5 container

    overlap_factor : float in range [0, 1] (default: 0.5)
        Block overlap size as a percentage of block size

    aligned_data : ndarray (default: None)
        A subpath in the zarr array to write the resampled data to

    transform_spacing : tuple
        Spacing to be applied for each transform. If not set
        it uses the fixed spacing

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
        The resampled moving data with transform_list applied. 
        If aligned_data is not None this will be the output
        Otherwise it returns a numpy array.
    """

    # get overlap and number of blocks
    blocksize_array = np.array(blocksize)
    nblocks = np.ceil(np.array(fix.shape) / blocksize_array).astype(int)
    overlaps = np.round(blocksize_array * overlap_factor).astype(int)

    # ensure there's a 1:1 correspondence between transform spacing 
    # and transform list
    if transform_spacing is None:
        # create transform spacing using same value as fixed image
        transform_spacing_list = ((np.array(fix_spacing),) * 
            len(transform_list))
    elif not isinstance(transform_spacing, tuple):
        # create a corresponding transform spacing for each transform
        transform_spacing_list = ((transform_spacing,) *
            len(transform_list))
    else:
        # transform spacing is a tuple
        # assume it's length matches transform list length
        transform_spacing_list = transform_spacing

    # prepare block coordinates
    blocks_coords = []
    for (i, j, k) in np.ndindex(*nblocks):
        start = blocksize_array * (i, j, k) - overlaps
        stop = start + blocksize_array + 2 * overlaps
        start = np.maximum(0, start)
        stop = np.minimum(fix.shape, stop)
        block_coords = tuple(slice(x, y) for x, y in zip(start, stop))
        blocks_coords.append(block_coords)

    print('Transform', len(blocks_coords), 'blocks',
          'with partition size' ,blocksize_array,
          flush=True)
    # align all blocks
    futures = cluster.client.map(
        _transform_single_block,
        blocks_coords,
        full_fix=fix,
        full_mov=mov,
        fix_spacing=fix_spacing,
        mov_spacing=mov_spacing,
        blocksize=blocksize_array,
        blockoverlaps=overlaps,
        transform_list=transform_list,
        transform_spacing_list=transform_spacing_list,
        *kwargs
    )

    for batch in as_completed(futures, with_results=True).batches():
        for _, result in batch:
            finished_block_coords, aligned_block = result

            print('Transformed block:',
                  finished_block_coords,
                  flush=True)

            if aligned_data is not None:
                print('Update warped block:',
                      finished_block_coords,
                      '(', aligned_block.shape, ')',
                      flush=True)
                aligned_data[finished_block_coords] = aligned_block
    

def _transform_single_block(block_coords,
                            full_fix=None,
                            full_mov=None,
                            fix_spacing=None,
                            mov_spacing=None,
                            blocksize=None,
                            blockoverlaps=None,
                            transform_list=[],
                            transform_spacing_list=[],
                            **additional_transform_args):
    """
    Block transform function
    """
    print('Transform block: ', block_coords, flush=True)
    # fetch fixed image slices and read fix
    fix_origin = fix_spacing * [s.start for s in block_coords]
    print('Block coords:',block_coords , 
          'Block origin:', fix_origin,
          'Block size:', blocksize,
          'Overlap:', blockoverlaps,
          flush=True)
    fix_block = full_fix[block_coords]

    # read relevant region of transforms
    applied_transform_list = []
    transform_origin = [fix_origin,] * len(transform_list)
    for iii, transform in enumerate(transform_list):
        if transform.shape != (4, 4):
            start = np.floor(fix_origin / transform_spacing_list[iii]).astype(int)
            stop = [s.stop for s in block_coords] * fix_spacing / transform_spacing_list[iii]
            stop = np.ceil(stop).astype(int)
            transform = transform[tuple(slice(a, b) for a, b in zip(start, stop))]
            transform_origin[iii] = start * transform_spacing_list[iii]
        applied_transform_list.append(transform)
    transform_origin = tuple(transform_origin)

    # transform fixed block corners, read moving data
    fix_block_coords = []
    for corner in list(product([0, 1], repeat=3)):
        a = [x.stop-1 if y else x.start for x, y in zip(block_coords, corner)]
        fix_block_coords.append(a)
    fix_block_coords = np.array(fix_block_coords) * fix_spacing

    mov_block_coords = cs_transform.apply_transform_to_coordinates(
        fix_block_coords,
        applied_transform_list,
        transform_spacing_list,
        transform_origin,
    )
    print('Transformed moving block coords:', block_coords, 
          fix_block_coords, '->', mov_block_coords,
          flush=True)

    mov_block_coords = np.round(mov_block_coords / mov_spacing).astype(int)
    mov_block_coords = np.maximum(0, mov_block_coords)
    mov_block_coords = np.minimum(full_mov.shape, mov_block_coords)
    print('Rounded transformed moving block coords:', block_coords, '->', mov_block_coords,
          flush=True)

    mov_start = np.min(mov_block_coords, axis=0)
    mov_stop = np.max(mov_block_coords, axis=0)
    mov_slices = tuple(slice(a, b) for a, b in zip(mov_start, mov_stop))
    mov_block = full_mov[mov_slices]
    mov_origin = mov_spacing * [s.start for s in mov_slices]
    print('Moving block origin:', fix_origin, '->', mov_origin,
          flush=True)
    print('Moving block coords:', block_coords, '->', mov_slices,
          flush=True)

    # resample
    aligned_block = cs_transform.apply_transform(
        fix_block, mov_block,
        fix_spacing, mov_spacing,
        transform_list=applied_transform_list,
        transform_origin=transform_origin,
        fix_origin=fix_origin,
        mov_origin=mov_origin,
        **additional_transform_args,
    )
    print('Warped block',
          block_coords, '->', mov_slices,
          'shape:', aligned_block.shape,
          flush=True)

    # crop out overlap
    final_block_coords_list = []
    for axis in range(aligned_block.ndim):
        # left side
        slc = [slice(None),]*aligned_block.ndim
        start = block_coords[axis].start
        stop = block_coords[axis].stop
        if block_coords[axis].start != 0:
            slc[axis] = slice(blockoverlaps[axis], None)
            print('Crop axis', axis, 'left', 
                  block_coords,'->',slc,
                  flush=True)
            aligned_block = aligned_block[tuple(slc)]
            start = start+blockoverlaps[axis]

        # right side
        slc = [slice(None),]*aligned_block.ndim
        if aligned_block.shape[axis] > blocksize[axis]:
            slc[axis] = slice(None, blocksize[axis])
            print('Crop axis', axis, 'right', block_coords,'->',slc,
                  flush=True)
            aligned_block = aligned_block[tuple(slc)]
            stop = start + aligned_block.shape[axis]

        final_block_coords_list.append(slice(start, stop))
    # convert the coords to a tuple
    final_block_coords = tuple(final_block_coords_list)
    print('Aligned block coords:', block_coords, '->', final_block_coords)
    # return result
    return final_block_coords, aligned_block


@cluster
def distributed_apply_transform_to_coordinates(
    coordinates,
    transform_list,
    voxel_blocksize,
    coords_spacing=None,
    coords_origin=None,
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

    voxel_blocksize : tuple
        The voxel block partition size used for distributing the work

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

    # determine partitions of coordinates
    phys_blocksize = np.array(voxel_blocksize)*coords_spacing
    min_coord = np.min(coordinates[:, 0:3], axis=0)
    max_coord = np.max(coordinates[:, 0:3], axis=0)
    vol_size = max_coord - min_coord
    nblocks = np.ceil(vol_size / phys_blocksize + 1).astype(int)
    print(f'{time.ctime(time.time())}',
          'Min coords:', min_coord,
          'Max coords:', min_coord,
          'Block size:', voxel_blocksize,
          'Phys block size:', phys_blocksize,
          'Vol size:', vol_size,
          'Voxel spacing:', coords_spacing,
          'NBlocks:', nblocks,
          flush=True)
    blocks_indexes = []
    blocks_slices = []
    blocks_origins = []
    blocks_points = []
    for (i, j, k) in np.ndindex(*nblocks):
        block_index = (i, j, k)
        block_start = voxel_blocksize * np.array(block_index)
        block_stop = block_start + voxel_blocksize
        block_slice_coords = tuple(slice(x, y) for x, y in zip(block_start, block_stop))
        lower_bound = min_coord + phys_blocksize * np.array(block_index)
        upper_bound = lower_bound + phys_blocksize
        print(f'{time.ctime(time.time())}',
              f'Get points for {block_index} from {lower_bound} to {upper_bound}',
                flush=True)
        not_too_low = np.all(coordinates[:, 0:3] >= lower_bound, axis=1)
        not_too_high = np.all(coordinates[:, 0:3] < upper_bound, axis=1)
        pcoords = coordinates[not_too_low * not_too_high]

        if pcoords.size > 0:
            print(f'{time.ctime(time.time())}',
                  f'Add {len(pcoords)} to block {block_index}',
                  flush=True)
            blocks_indexes.append(block_index)
            blocks_slices.append(block_slice_coords)
            blocks_origins.append(lower_bound)
            blocks_points.append(pcoords)
        else:
            print(f'{time.ctime(time.time())}',
                  f'No point added to block {block_index}',
                  flush=True)
    if len(blocks_indexes) > 0:
        # transform all partitions and return
        futures = cluster.client.map(
            _transform_coords,
            blocks_indexes,
            blocks_slices,
            blocks_origins,
            blocks_points,
            coords_spacing=coords_spacing,
            transform_list=transform_list,
        )
        results = cluster.client.gather(futures)
    else:
        results = []
    if len(results) > 0:
        return np.concatenate(results, axis=0)
    else:
        return np.empty((0,3))


def _transform_coords(block_index,
                      block_slice_coords,
                      block_origin,
                      coord_indexed_values,
                      coords_spacing=None,
                      transform_list=[]):
    # read relevant region of transform
    print(f'{time.ctime(time.time())} Apply block transform: ', block_index,
          'block origin', block_origin,
          'block slice coords', block_slice_coords,
          'to', len(coord_indexed_values), 'points',
          flush=True)

    points_coords = coord_indexed_values[:, 0:3]
    points_values = coord_indexed_values[:, 3:]

    cropped_transforms = []
    for _, transform in enumerate(transform_list):
        if transform.shape != (4, 4):
            crop_slices = []
            for axis in range(transform.ndim-1):
                start = block_slice_coords[axis].start
                stop = block_slice_coords[axis].stop
                if transform.shape[axis] > stop:
                    crop_slices.append(slice(start, transform.shape[axis]))
                else:
                    crop_slices.append(slice(start, stop))
            print(f'{time.ctime(time.time())} Crop block {block_index} transform: ',
                f'to {crop_slices}',
                flush=True)
            # for vector displacement fields crop the transformation
            cropped_transforms.append(transform[tuple(crop_slices)])
        else:
            # no need to do any cropping if it's an affine matrix
            cropped_transforms.append(transform)

    # apply transforms
    warped_coords = cs_transform.apply_transform_to_coordinates(
        points_coords,
        cropped_transforms,
        transform_spacing=coords_spacing,
        transform_origin=block_origin
    )

    warped_coord_indexed_values = np.empty_like(coord_indexed_values)
    warped_coord_indexed_values[:, 0:3] = warped_coords
    warped_coord_indexed_values[:, 3:] = points_values

    max_warped_coord = np.max(warped_coord_indexed_values[:, 0:3], axis=0)

    print(f'{time.ctime(time.time())} Finished block: ', block_index,
          f'- warped {warped_coord_indexed_values.shape} coords',
          f'max warped coord {max_warped_coord}',
          flush=True)

    return warped_coord_indexed_values


@cluster
def distributed_invert_displacement_vector_field(
    vectorfield_array,
    spacing,
    blocksize,
    inv_vectorfield_array,
    overlap_factor=0.25,
    iterations=10,
    order=2,
    sqrt_iterations=5,
    cluster=None,
    cluster_kwargs={},
):
    """
    Numerically find the inverse of a larger-than-memory displacement vector field

    Parameters
    ----------
    vectorfield_array : zarr array
        The displacement vector field to invert

    spacing : 1d-array
        The physical voxel spacing of the displacement field

    blocksize : tuple
        The shape of blocks in voxels

    inv_vectorfield_array : zarr array
        The inverse vector field

    overlap_factor : overlap factor (default: 0.25)

    iterations : scalar int (default: 10)
        The number of stationary point iterations to find inverse. More
        iterations gives a more accurate inverse but takes more time.

    order : scalar int (default: 2)
        The number of roots to take before stationary point iterations.

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

    """

    # get overlap and number of blocks
    blocksize_array = np.array(blocksize)
    overlap = np.round(blocksize_array * overlap_factor).astype(int)
    nblocks = np.ceil(np.array(vectorfield_array.shape[:-1]) / 
                      blocksize_array).astype(int)

    # store block coordinates in a dask array
    blocks_coords = []
    for (i, j, k) in np.ndindex(*nblocks):
        start = blocksize_array * (i, j, k) - overlap
        stop = start + blocksize_array + 2 * overlap
        start = np.maximum(0, start)
        stop = np.minimum(vectorfield_array.shape[:-1], stop)
        coords = tuple(slice(x, y) for x, y in zip(start, stop))
        blocks_coords.append(coords)

    # invert all blocks
    print(f'{time.ctime(time.time())} Invert', len(blocks_coords), 'blocks',
          'with partition size', blocksize_array,
          flush=True)

    invert_res = cluster.client.map(
        _invert_block,
        blocks_coords,
        full_vectorfield=vectorfield_array,
        spacing=spacing,
        blocksize=blocksize_array,
        blockoverlaps=overlap,
        iterations=iterations,
        order=order,
        sqrt_iterations=sqrt_iterations,
    )

    write_invert_res = cluster.client.map(
        _write_block,
        invert_res,
        output=inv_vectorfield_array
    )

    for batch in as_completed(write_invert_res, with_results=True).batches():
        for _, result in batch:
            block_coords = result

            print(f'{time.ctime(time.time())} Finished inverting block:',
                  block_coords,
                  flush=True)


def _invert_block(block_coords,
                  full_vectorfield=None,
                  spacing=None,
                  blocksize=None,
                  blockoverlaps=None,
                  iterations=10,
                  order=2,
                  sqrt_iterations=5):
    """
    Invert block function
    """
    print('Invert block:', block_coords, flush=True)

    block_vectorfield = full_vectorfield[block_coords]
    inverse_block = cs_transform.invert_displacement_vector_field(
        block_vectorfield, spacing, iterations, order, sqrt_iterations,
    )

    print('Computed inverse field for block', 
          block_coords, block_vectorfield.shape,
          '->',
          inverse_block.shape,
          flush=True)
    # crop out overlap
    inverse_block_coords_list = []
    for axis in range(inverse_block.ndim - 1):
        # left side
        slc = [slice(None),]*(inverse_block.ndim - 1)
        start = block_coords[axis].start
        stop = block_coords[axis].stop
        if block_coords[axis].start != 0:
            slc[axis] = slice(blockoverlaps[axis], None)
            inverse_block = inverse_block[tuple(slc)]
            start = start+blockoverlaps[axis]

        # right side
        slc = [slice(None),]*(inverse_block.ndim - 1)
        if inverse_block.shape[axis] > blocksize[axis]:
            slc[axis] = slice(None, blocksize[axis])
            inverse_block = inverse_block[tuple(slc)]
            stop = start + inverse_block.shape[axis]

        inverse_block_coords_list.append(slice(start, stop))

    inverse_block_coords = tuple(inverse_block_coords_list)
    print('Completed inverse vector field for block', 
          block_coords, block_vectorfield.shape,
          '->',
          inverse_block_coords, inverse_block.shape,
          flush=True)
    # return result
    return inverse_block_coords, inverse_block


def _write_block(block, output=None):
    block_coords, block_data = block

    if output is not None:
        print(f'{time.ctime(time.time())} Write block:',
                block_coords,
                '(', block_data.shape, ')',
                flush=True)
        output[block_coords] = block_data

    return block_coords
