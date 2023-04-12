###############################################################################
# Copyright 2017-2023 - Climate Research Division
#                       Environment and Climate Change Canada
#
# This file is part of the "fstd2nc" package.
#
# "fstd2nc" is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# "fstd2nc" is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with "fstd2nc".  If not, see <http://www.gnu.org/licenses/>.
###############################################################################

from fstd2nc.stdout import _, info, warn, error
from fstd2nc.mixins import BufferBase
try:
  from collections import Callable
except ImportError:  # Python 3.10
  from collections.abc import Callable

#################################################
# Provide various external array interfaces for the FSTD data.

# Helper interface for ordering dask tasks based on FSTD record order.
# Might provide a small speed boost when the OS employs a read-ahead buffer.
# Based on dask.core.get_dependencies
class _RecordOrder (object):
  def __init__(self, dsk):
    self.dask = dsk
  def __call__ (self, arg):
    dsk = self.dask
    work = [arg]
    while work:
        new_work = []
        for w in work:
            typ = type(w)
            if typ is tuple and w and isinstance(w[0], Callable):  # istask(w)
                if w[0] is _read_block:
                  return w[1], w[2]
                else:
                  new_work += w[1:]
            elif typ is list:
                new_work += w
            elif typ is dict:
                new_work += list(w.values())
            else:
                try:
                    if w in dsk:
                        new_work.append(dsk[w])
                except TypeError:  # not hashable
                    pass
        work = new_work

# Add a callback to dask to ensure FSTD records are read in a good order.
try:
  from dask.callbacks import Callback
  class _FSTD_Callback (Callback):
    def _start_state (self, dsk, state):
      # Try sorting by FSTD filename, offset (if applicable)
      try:
        ready = sorted(state['ready'][::-1],key=_RecordOrder(dsk))
        state['ready'] = ready[::-1]
      except TypeError: pass  # Not applicable for this graph.
  _FSTD_Callback().register()
  del Callback

except ImportError:
  pass


# Method for reading a block from a file.
def _read_block (filename, offset, length):
  import numpy as np
  with open(filename,'rb') as f:
    f.seek (offset,0)
    return np.fromfile(f,'B',length)


class ExternOutput (BufferBase):

  # Helper method to get graph for raw input data.
  def _graphs (self):
    import numpy as np
    import pandas as pd
    graphs = [None] * self._nrecs
    ###
    # Special case: already have a dask object from external source.
    # (E.g., from fstpy)
    if hasattr(self, '_extern_table'):
      for rec_id in range(self._nrecs):
        d = self._extern_table['d'].iloc[rec_id]
        if hasattr(d,'compute'):
          # Start a dask graph using the external dask array as the source.
          graph = (d.compute,)
          graph = (np.ravel, graph, 'K')
        else:  # Special case: have a numpy array in memory.
          graph = d
          graph = np.ravel(graph, 'K')
        graphs[rec_id] = graph
      return graphs
    ###
    # Otherwise, construct graphs with our own dask wrapper.
    files = np.array(self._files, dtype=object)
    # Only construct graphs for records that will ultimately appear in the
    # dask objects.
    active = self._headers['selected'] == True
    nrecs = np.sum(active)
    filenames = files[self._headers['file_id'][active]]
    graphs = zip([_read_block]*nrecs, filenames, self._headers['address'][active], self._headers['length'][active])
    graphs = zip([self._decode]*nrecs, graphs, np.where(active)[0])
    g = np.empty(nrecs, dtype=object)
    g[:] = list(graphs)
    out = np.empty(self._nrecs, dtype=object)
    out[active] = g
    return out

  def _iter_dask (self, include_coords=True):
    """
    Iterate over all the variables, and convert to dask arrays.
    """
    from fstd2nc.mixins import _iter_type, _chunk_type, _var_type
    from dask import array as da
    from dask.base import tokenize
    import numpy as np
    from itertools import product
    from dask import delayed
    unique_token = tokenize(self._files,id(self))
    graphs = self._graphs()
    self._makevars()
    for var in self._iter_objects():
      if not include_coords:
        if var not in self._varlist:
          continue
      if not isinstance(var,(_iter_type,_chunk_type)):
        yield var
        continue
      name = var.name+"-"+unique_token
      ndim = len(var.axes)
      shape = var.shape
      # Convert _iter_type to more generic _chunk_type.
      if isinstance(var,_iter_type):
        chunks = {}
        ndim_outer = var.record_id.ndim
        ndim_inner = ndim - ndim_outer
        chunk_shape = shape[ndim_outer:]
        for ind in product(*map(range,var.record_id.shape)):
          rec_id = var.record_id[ind]
          ind = ind + tuple((0,dx) for dx in shape[ndim_outer:])
          chunks[ind] = rec_id
        var = _chunk_type (var.name, var.atts, var.axes, var.dtype, chunks, chunk_shape)
      # Convert _chunk_type to dask Array objects.
      if isinstance(var,_chunk_type):
        ndim_inner = len(var.chunksize)
        ndim_outer = ndim - ndim_inner
        # Get chunk dimensions.
        # First, size of single (untruncated) chunk, full indices.
        untruncated_chunksize = (1,)*(ndim-len(var.chunksize)) + var.chunksize
        # Next, breakdown of chunks along all variable dimensions.
        chunks = []
        chunk_indices = []
        for i in range(ndim):
          dx = untruncated_chunksize[i]
          ch = tuple(dx for j in range(dx,shape[i]+1,dx))
          if shape[i] % dx > 0:
            ch = ch + (shape[i] % dx, )
          chunks.append(ch)
          chunk_indices.append(range(len(ch)))
        # Loop over all indices, generate dask graph.
        dsk = dict()
        for ind, chunk_shape in zip(product(*chunk_indices), product(*chunks)):
          # Unique key for this graph member.
          key = (name,) + ind
          # Get record id.
          slices = [(i*dx,i*dx+res) for i,dx,res in zip(ind,untruncated_chunksize,chunk_shape)]
          slices[:ndim_outer] = ind[:ndim_outer]
          rec_id = var.chunks.get(tuple(slices),-1)
          # Add this record as a chunk in the dask Array.
          # Also, specify the preferred order of reading the chunks within the
          # file.
          if rec_id >= 0:
            graph = graphs[rec_id]
            dsk[key] = (np.reshape, graph, chunk_shape)
          else:
            # Fill missing chunks with fill value or NaN.
            if hasattr(self,'_fill_value'):
              var.atts['_FillValue'] = self._fill_value
              dsk[key] = (np.full, chunk_shape, self._fill_value, var.dtype)
            else:
              dsk[key] = (np.full, chunk_shape, float('nan'), var.dtype)
        array = da.Array(dsk, name, chunks, var.dtype)
        var = _var_type(var.name,var.atts,var.axes,array)
      yield var

  def to_xarray (self):
    """
    Create an xarray interface for the RPN data.
    Requires the xarray and dask packages.
    """
    from collections import OrderedDict
    import xarray as xr
    out = OrderedDict()
    for var in self._iter_dask():
      if not hasattr(var,'array'): continue
      out[var.name] = xr.DataArray(data=var.array, dims=var.dims, name=var.name, attrs=var.atts)
      # Preserve chunking information for writing to netCDF4.
      if hasattr(var.array,'chunks'):
        chunk_shape = [c[0] for c in var.array.chunks]
        out[var.name].encoding['chunksizes'] = chunk_shape
        out[var.name].encoding['original_shape'] = out[var.name].shape

    # Construct the Dataset from all the variables.
    out = xr.Dataset(out)
    # Decode CF metadata
    out = xr.conventions.decode_cf(out)

    # Make the time dimension unlimited when writing to netCDF.
    out.encoding['unlimited_dims'] = ('time',)

    return out

  def to_xarray_list (self):
    """
    Similar to the to_xarray method, but returns a list of xarray Datasets,
    one for each variable, instead of a single Dataset object.
    Could be useful for case where the dimension names are non-unique, to avoid
    name clobbering (in conjunction with unique_names initialization option).
    E.g.,
    Buffer("filename",unique_names=False).to_xarray_list()
    """
    from collections import OrderedDict
    import xarray as xr
    from fstd2nc.mixins import _axis_type
    out_list = []
    for var in self._iter_dask(include_coords=False):
      if not hasattr(var,'array'): continue
      out = OrderedDict()
      out[var.name] = xr.DataArray(data=var.array, dims=var.dims, name=var.name, attrs=var.atts)
      for extra in self._iter_objects(var):
        if not hasattr(extra,'array'): continue
        out[extra.name] = xr.DataArray(data=extra.array, dims=extra.dims, name=extra.name, attrs=extra.atts)
      # Preserve chunking information for writing to netCDF4.
      if hasattr(var.array,'chunks'):
        chunk_shape = [c[0] for c in var.array.chunks]
        out[var.name].encoding['chunksizes'] = chunk_shape
        out[var.name].encoding['original_shape'] = out[var.name].shape
      # Construct the Dataset from the variable.
      out = xr.Dataset(out)
      # Decode CF metadata
      out = xr.conventions.decode_cf(out)
      # Make the time dimension unlimited when writing to netCDF.
      out.encoding['unlimited_dims'] = ('time',)
      out_list.append(out)

    return out_list

  def to_iris (self):
    """
    Create an iris interface for the RPN data.
    Requires iris >= 2.0, xarray >= 0.10.3, and dask.
    Returns a CubeList object.
    """
    from iris.cube import CubeList
    out = []
    for var in self.to_xarray().data_vars.values():
      # Omit some problematic variables.
      if var.dtype == '|S1': continue
      # Need to clean up some unrecognized metadata.
      for coord in var.coords.values():
        # Remove units of 'level' (confuses cf_units).
        if coord.attrs.get('units',None) in ('level','sigma_level'):
          coord.attrs.pop('units')
        # Remove non-standard standard names.
        if coord.attrs.get('standard_name',None) == 'atmosphere_hybrid_sigma_ln_pressure_coordinate':
          coord.attrs.pop('standard_name')
      out.append(var.to_iris())
    return CubeList(out)

  def to_pygeode (self):
    """
    Create a pygeode interface for the RPN data.
    Requires pygeode >= 1.2.0, and xarray/dask.
    """
    _fix_to_pygeode()
    from pygeode.ext_xarray import from_xarray
    data = self.to_xarray()
    return from_xarray(data)

  def to_fstpy (self):
    """
    Create a table compatible with the fstpy module.
    Requires pandas and dask.
    """
    import pandas as pd
    import numpy as np
    from fstpy.dataframe import add_grid_column
    from fstpy.std_io import add_dask_column
    # Special case: our data is already from an fstpy table, not from an FSTD
    # file in our control.
    # E.g., if some smartass does Buffer.from_fstpy(df).to_fstpy()
    if hasattr(self, '_extern_table'):
      return self._extern_table
    # Put all the header info into a dictionary.
    fields = ['nomvar', 'typvar', 'etiket', 'ni', 'nj', 'nk', 'dateo', 'ip1', 'ip2', 'ip3', 'deet', 'npas', 'datyp', 'nbits', 'grtyp', 'ig1', 'ig2', 'ig3', 'ig4', 'datev']
    table = dict()
    # Create a mask to exclude deleted / overwritten / unselected records.
    # Include all meta (coordinate) records in the output.
    mask = self._headers['selected'] | self._headers['ismeta']
    for field in fields:
      col = self._headers[field][mask]
      # Convert byte arrays to strings, which is what fstpy expects.
      if col.dtype.str.startswith('|S'):
        col = np.asarray(col,dtype=col.dtype.str.replace('|S','<U'))
      table[field] = col
    # Convert to pandas table.
    table = pd.DataFrame.from_dict(table)
    # Add grid info.
    add_grid_column (table)
    # Temporarily insert some extra columns needed for the data.
    table['shape'] = list(zip(table['ni'],table['nj']))
    filenames = dict((i,f) for i,f in enumerate(self._files))
    table['path'] = pd.Series(self._headers['file_id'][mask]).map(filenames)
    key = np.zeros(len(self._headers['name']),'int32')
    for file_id in range(len(self._files)):
      selection = self._headers['file_id'] == file_id
      indices = np.arange(np.sum(selection), dtype='int32')
      key[selection] = (indices % 256) | ((indices//256)<<9)
    table['key'] = key[mask] << 10
    # Generate dask objects
    #TODO: use our own, in case we modified the data?
    # (doesn't normally happen, but you never know...)
    # For instance could happen if interp is used.
    add_dask_column(table)
    # Clean up temporary columns and return.
    table.drop(columns=['shape','path','key'], inplace=True)
    return table

# Workaround for recent xarray (>0.10.0) which changed the methods in the
# conventions module.
# Fixes an AttributeError when using to_pygeode().
def _fix_to_pygeode (fixed=[False]):
  if fixed[0] is True: return
  try:
    from xarray.coding import times
    from xarray import conventions
    if not hasattr(conventions,'maybe_encode_datetime'):
      conventions.maybe_encode_datetime = times.CFDatetimeCoder().encode
    if not hasattr(conventions,'maybe_encode_timedelta'):
      conventions.maybe_encode_timedelta = times.CFTimedeltaCoder().encode
  except (ImportError,AttributeError):
    pass
  fixed[0] = True

class ExternInput (BufferBase):
  def __init__ (self, *args, **kwargs):
    if '_extern_table' in kwargs:
      self._extern_table = kwargs.pop('_extern_table')
    super(ExternInput,self).__init__(*args,**kwargs)
  @classmethod
  def from_fstpy (cls, table, **kwargs):
    import numpy as np
    if hasattr(table,'to_pandas'):
      table = table.to_pandas()
    # Construct the record header info from the table.
    fields = ['nomvar', 'typvar', 'etiket', 'ni', 'nj', 'nk', 'dateo', 'ip1', 'ip2', 'ip3', 'deet', 'npas', 'datyp', 'nbits', 'grtyp', 'ig1', 'ig2', 'ig3', 'ig4', 'datev']
    headers = {}
    for col in fields:
      headers[col] = table[col].values.copy()
    # Pad out string variables with spaces.
    headers['nomvar'] = np.asarray(headers['nomvar'], dtype='|S4')
    headers['typvar'] = np.asarray(headers['nomvar'], dtype='|S2')
    headers['etiket'] = np.asarray(headers['etiket'], dtype='|S12')
    headers['grtyp'] = np.asarray(headers['grtyp'], dtype='|S1')
    headers['nomvar'] = np.char.ljust(headers['nomvar'], 4, ' ')
    headers['typvar'] = np.char.ljust(headers['typvar'], 2, ' ')
    headers['etiket'] = np.char.ljust(headers['etiket'], 12, ' ')
    # Add other fields that may be needed.
    if 'dltf' not in headers:
      headers['dltf'] = np.zeros(len(headers['nomvar']), dtype='int32')
      # We don't have file address info, so mark this as 'None' in case
      # any subroutine is looking for this info.
      headers['address'] = np.empty(len(headers['nomvar']), dtype=object)
      headers['length'] = np.empty(len(headers['nomvar']), dtype=object)
    # Fake file id (just so netcdf mixin _quick_load function doesn't crash).
    headers['file_id'] = np.zeros(len(headers['nomvar']), dtype='int32')

    # Encapsulate this info in a structure.
    fake_buffer = cls.__new__(cls)
    fake_buffer._files = [None]
    fake_buffer._headers = headers

    # Initialize a Buffer object with this info.
    # Also save the dataframe for reference.
    # Will need the dask objects for getting the data.
    b = cls(fake_buffer, _extern_table=table, **kwargs)

    return b

  # Handle external data for read_record method.
  # Use data from _extern_table.
  def _read_record (self, rec_id):
    import numpy as np
    # Check if there is custom data enabled for this Buffer.
    if hasattr(self, '_extern_table'):
      # Extract the record info from the table.
      rec = self._extern_table.iloc[rec_id].to_dict()
      # Load the data (if delayed).
      rec['d'] = np.asarray(rec['d'])
      return rec['d'].T
    # Otherwise, continue as usual.
    return super(ExternInput,self)._read_record (rec_id)

  # Handle external data for _decode method.
  # In this case, the first argument is ignored (no file data was read).
  def _decode (self, maybe_data, rec_id):
    import numpy as np
    # Check if there is custom data enabled for this Buffer.
    if hasattr(self, '_extern_table'):
      # Extract the record info from the table.
      rec = self._extern_table.iloc[rec_id].to_dict()
      # Load the data (if delayed).
      rec['d'] = np.asarray(rec['d'])
      return rec['d'].T
    # Otherwise, continue as usual.
    return super(ExternInput,self)._decode (maybe_data, rec_id)

