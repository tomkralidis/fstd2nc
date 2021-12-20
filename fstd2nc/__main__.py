###############################################################################
# Copyright 2017-2021 - Climate Research Division
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


from __future__ import print_function
from fstd2nc.stdout import _, info, warn, error
from fstd2nc import Buffer


#################################################
# Dump the metadata for an RPN standard file to stdout (without conversion
# step).  Displays in format similar to "ncdump" for netCDF files.
def _fstdump (buffer_type=Buffer):
  import fstd2nc
  from argparse import ArgumentParser
  from rpnpy.librmn.fstd98 import FSTDError, fstopt
  from os.path import basename
  import textwrap
  parser = ArgumentParser(description=_("Display RPN standard file (FSTD) metadata in a structured format."))
  parser.add_argument('infile', metavar='<fstd_file>', help=_('The RPN standard file to query.'))
  parser.add_argument('-v', nargs='*', metavar='NOMVAR,...', help=_('Display the values for the specified variable.'))
  buffer_type._cmdline_args(parser)
  args = parser.parse_args()
  buffer_type._check_args(parser, args)
  args = vars(args)
  infile = args.pop('infile')
  nomvars = args.pop('v')

  # Apply message level criteria.
  fstopt ('MSGLVL',6)
  fstd2nc.stdout.streams = ('error',)

  try:
    buf = buffer_type(infile, **args)
  except FSTDError:
    error (_("problem opening one or more input files."))

  # Get the metadata in a netCDF-like structure.
  buf._makevars()
  print ("fstd98 %s {"%basename(infile))
  print ("dimensions:")
  for axis in buf._iter_axes():
    print ("\t%s = %d ;"%(axis.name,len(axis)))
  print ("variables:")
  for var in buf._iter_objects():
    if not hasattr(var,'axes'): continue # Skip dimensions
    if hasattr(var,'dtype'):
      dtype = var.dtype
    elif hasattr(var,'array'):
      dtype = var.array.dtype
    else:
      dtype = '???'
    if len(var.axes) == 0:
      print ("\t%s %s ;"%(dtype,var.name))
    else:
      print ("\t%s %s(%s) ;"%(dtype,var.name,", ".join(var.dims)))
    for att,val in var.atts.items():
      # Skip fill value (not coming from the file on disk).
      if att == "_FillValue": continue
      if isinstance(val,str):
        s = '"'+val+'"'
      else:
        s = str(val)
      print ("\t\t%s:%s = %s ;"%(var.name,att,s))

  # Print variable data.
  if nomvars is not None:
    print ("data:")
    nomvars = [n for nomvar in nomvars for n in nomvar.split(',')]
    for var in buf._iter_objects():
      if var.name in nomvars:
        if hasattr(var,'array'):
           values = var.array
        else:
           continue
        # Build string values?
        strlen = None
        for idim,dimname in enumerate(var.dims):
          if dimname.endswith('_strlen'):
            strlen = len(var.axes[idim])
        if strlen is not None:
          values = values.view('|S%s'%strlen)
        if 'bytes' in values.dtype.name:
          values = values.astype(str).flatten()
          values = '"' + '", "'.join(values) + '"'
        else:
          values = values.flatten()
          values = map(str,values)
          values = ', '.join(values)
        lines = textwrap.wrap('%s = %s ;'%(var.name, values), initial_indent=' ', subsequent_indent='    ')
        print ()
        for line in lines:
          print (line)

  print ("}")


#################################################
# Command-line invocation of the converter.
def _fstd2nc_cmdline (buffer_type=Buffer):
  from argparse import ArgumentParser
  from sys import stdout, argv
  from os.path import exists
  from rpnpy.librmn.fstd98 import FSTDError, fstopt
  parser = ArgumentParser(description=_("Converts an RPN standard file (FSTD) to netCDF format."))
  parser.add_argument('infile', nargs='+', metavar='<fstd_file>', help=_('The RPN standard file(s) to convert.'))
  parser.add_argument('outfile', metavar='<netcdf_file>', help=_('The name of the netCDF file to create.'))
  buffer_type._cmdline_args(parser)
  parser.add_argument('--msglvl', choices=['0','DEBUG','2','INFORM','4','WARNIN','6','ERRORS','8','FATALE','10','SYSTEM','CATAST'], default='WARNIN', help=_('How much information to print to stdout during the conversion.  Default is %(default)s.'))
  parser.add_argument('--nc-format', choices=['NETCDF4','NETCDF4_CLASSIC','NETCDF3_CLASSIC','NETCDF3_64BIT_OFFSET','NETCDF3_64BIT_DATA'], default='NETCDF4', help=_('Which variant of netCDF to write.  Default is %(default)s.'))
  parser.add_argument('--zlib', action='store_true', help=_("Turn on compression for the netCDF file.  Only works for NETCDF4 and NETCDF4_CLASSIC formats."))
  parser.add_argument('--compression', type=int, default=4, help=_("Compression level for the netCDF file. Only used if --zlib is set. Default: %(default)s."))
  parser.add_argument('-f', '--force', action='store_true', help=_("Overwrite the output file if it already exists."))
  parser.add_argument('--no-history', action='store_true', help=_("Don't put the command-line invocation in the netCDF metadata."))
  args = parser.parse_args()
  buffer_type._check_args(parser, args)
  args = vars(args)
  infiles = args.pop('infile')
  outfile = args.pop('outfile')
  msglvl = args.pop('msglvl')
  nc_format = args.pop('nc_format')
  zlib = args.pop('zlib')
  force = args.pop('force')
  no_history = args.pop('no_history')
  compression = args.pop('compression')
  progress = args.get('progress',False)

  # Apply message level criteria.
  try:
    msglvl = int(msglvl)
  except ValueError:
    msglvl = {'DEBUG':0,'INFORM':2,'WARNIN':4,'ERRORS':6,'FATALE':8,'SYSTEM':10,'CATAST':10}[msglvl]
  fstopt ('MSGLVL',msglvl)

  try:
    buf = buffer_type(infiles, **args)
  except FSTDError:
    error (_("problem opening one or more input files."))

  # Check if output file already exists
  if exists(outfile) and not force:
    overwrite = False
    if stdout.isatty():
      while True:
        print (_("Warning: '%s' already exists!  Overwrite? (y/n):")%(outfile), end=' ')
        try: ans = raw_input()
        except NameError: ans = input()
        if ans.lower() in ('y','yes','o','oui'):
          overwrite = True
          break
        if ans.lower() in ('n','no','non'):
          overwrite = False
          break
        print (_("Sorry, invalid response."))
    if overwrite is False:
      error (_("Refusing to overwrite existing file '%s'.")%(outfile))

  # Append the command invocation to the netCDF metadata?
  if no_history:
    global_metadata = None
  else:
    from datetime import datetime
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    command = list(argv)
    # Any arguments with spaces should be surrounded by quotes.
    for i,c in enumerate(command):
      if " " in c:
        command[i] = "'"+c+"'"
    command = " ".join(command)
    history = timestamp + ": " + command
    global_metadata = {"history":history}

  buf.to_netcdf(outfile, nc_format=nc_format, global_metadata=global_metadata, zlib=zlib, compression=compression, progress=progress)

#################################################
# Command-line invocation with error trapping.
# Hides the Python stack trace when the user aborts the command.
def _fstd2nc_cmdline_trapped (*args, **kwargs):
  try:
    _fstd2nc_cmdline (*args, **kwargs)
  except KeyboardInterrupt:
    error (_("Aborted by user."))

if __name__ == '__main__':
  _fstd2nc_cmdline_trapped()

