"""TFRecord sources and sinks."""

import logging
import struct

import apache_beam as beam
from apache_beam import coders
from apache_beam.io import filebasedsource

import crcmod

# pylint: disable=g-import-not-at-top
try:
  # TODO(user): Remove this after updating to latest Beam.
  from apache_beam.io import filebasedsink
except ImportError:
  from apache_beam.io import fileio as filebasedsink

try:
  # TODO(user): Remove this after updating to latest Beam.
  from apache_beam.io.filebasedsink import FileBasedSink
except ImportError:
  from apache_beam.io.fileio import FileSink
  FileBasedSink = FileSink

try:
  # TODO(user): Remove this after updating to latest Beam.
  from apache_beam.io.filesystem import CompressionTypes
except ImportError:
  from apache_beam.io.fileio import CompressionTypes

try:
  # TODO(user): Remove this after updating to latest Beam.
  from apache_beam.io import Read
except ImportError:
  from apache_beam import Read

_crc32c_fn = None
try:
  import snappy  # pylint: disable=g-import-not-at-top
  _crc32c_fn = snappy._crc32c  # pylint: disable=protected-access
except ImportError:
  logging.warning('Couldn\'t find python-snappy so the implementation of '
                  '_TFRecordUtil._masked_crc32c is not as fast as it could be.')
  _crc32c_fn = crcmod.predefined.mkPredefinedCrcFun('crc-32c')


class _TFRecordUtil(object):
  """Provides basic TFRecord encoding/decoding with consistency checks.

  For detailed TFRecord format description see:
    https://www.tensorflow.org/versions/master/api_docs/python/python_io.html#tfrecords-format-details

  Note that masks and length are represented in LittleEndian order.
  """

  # This code is performance critical so make sure to run the corresponding
  # benchmark when updating it.
  @classmethod
  def _masked_crc32c(cls, value, crc32c_fn=_crc32c_fn):
    """Compute a masked crc32c checksum for a value.

    Args:
      value: A string for which we compute the crc.
      crc32c_fn: A function that can compute a crc32c.
        This is a performance hook that also helps with testing. Callers are
        not expected to make use of it directly.
    Returns:
      Masked crc32c checksum.
    """
    crc = crc32c_fn(value)
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff

  @staticmethod
  def encoded_num_bytes(record):
    """Return the number of bytes consumed by a record in its encoded form."""
    # 16 = 8 (Length) + 4 (crc of length) + 4 (crc of data)
    return len(record) + 16

  @classmethod
  def write_record(cls, file_handle, value):
    """Encode a value as a TFRecord.

    Args:
      file_handle: The file to write to.
      value: A string content of the record.
    """
    encoded_length = struct.pack('<Q', len(value))
    file_handle.write('{}{}{}{}'.format(
        encoded_length,
        struct.pack('<I', cls._masked_crc32c(encoded_length)),  #
        value,
        struct.pack('<I', cls._masked_crc32c(value))))

  @classmethod
  def read_record(cls, file_handle):
    """Read a record from a TFRecord file.

    Args:
      file_handle: The file to read from.
    Returns:
      None if EOF is reached; the paylod of the record otherwise.
    Raises:
      ValueError: If file appears to not be a valid TFRecord file.
    """
    buf_length_expected = 12
    buf = file_handle.read(buf_length_expected)
    if not buf:
      return None  # EOF Reached.

    # Validate all length related payloads.
    if len(buf) != buf_length_expected:
      raise ValueError('Not a valid TFRecord. Fewer than %d bytes: %s' %
                       (buf_length_expected, buf.encode('hex')))
    length, length_mask_expected = struct.unpack('<QI', buf)
    length_mask_actual = cls._masked_crc32c(buf[:8])
    if length_mask_actual != length_mask_expected:
      raise ValueError('Not a valid TFRecord. Mismatch of length mask: %s' %
                       buf.encode('hex'))

    # Validate all data related payloads.
    buf_length_expected = length + 4
    buf = file_handle.read(buf_length_expected)
    if len(buf) != buf_length_expected:
      raise ValueError('Not a valid TFRecord. Fewer than %d bytes: %s' %
                       (buf_length_expected, buf.encode('hex')))
    data, data_mask_expected = struct.unpack('<%dsI' % length, buf)
    data_mask_actual = cls._masked_crc32c(data)
    if data_mask_actual != data_mask_expected:
      raise ValueError('Not a valid TFRecord. Mismatch of data mask: %s' %
                       buf.encode('hex'))

    # All validation checks passed.
    return data


class _TFRecordSource(filebasedsource.FileBasedSource):
  """A File source for reading files of TFRecords.

  For detailed TFRecord format description see:
    https://www.tensorflow.org/versions/master/api_docs/python/python_io.html#tfrecords-format-details
  """

  def __init__(self,
               file_pattern,
               coder,
               compression_type):
    """Initialize a TFRecordSource.  See ReadFromTFRecord for details."""
    super(_TFRecordSource, self).__init__(
        file_pattern=file_pattern,
        compression_type=compression_type,
        splittable=False)
    self._coder = coder

  def read_records(self, file_name, offset_range_tracker):
    if offset_range_tracker.start_position():
      raise ValueError('Start position not 0:%s' %
                       offset_range_tracker.start_position())

    current_offset = offset_range_tracker.start_position()
    with self.open_file(file_name) as file_handle:
      while True:
        if not offset_range_tracker.try_claim(current_offset):
          raise RuntimeError('Unable to claim position: %s' % current_offset)
        record = _TFRecordUtil.read_record(file_handle)
        if record is None:
          return  # Reached EOF
        else:
          current_offset += _TFRecordUtil.encoded_num_bytes(record)
          yield self._coder.decode(record)


class ReadFromTFRecord(beam.PTransform):
  """Transform for reading TFRecord sources."""

  def __init__(self,
               file_pattern,
               coder=coders.BytesCoder(),
               compression_type=CompressionTypes.AUTO,
               **kwargs):
    """Initialize a ReadFromTFRecord transform.

    Args:
      file_pattern: The file pattern to read from as a local file path or a GCS
        gs:// path. The path can contain glob characters (*, ?, and [...]
        sets).
      coder: Coder used to decode each record.
      compression_type: Used to handle compressed input files. Typical value
          is CompressionTypes.AUTO, in which case the file_path's extension will
          be used to detect the compression.
      **kwargs: optional args dictionary. These are passed through to parent
        constructor.

    Returns:
      A WriteToTFRecord transform object.
    """
    super(ReadFromTFRecord, self).__init__(**kwargs)
    self._args = (file_pattern, coder, compression_type)

  # TODO(b/33677990): Remove apply method.
  def apply(self, pvalue):
    return self.expand(pvalue)

  def expand(self, pvalue):
    return pvalue.pipeline | Read(_TFRecordSource(*self._args))


class _TFRecordSink(FileBasedSink):
  """Sink for writing TFRecord files.

  For detailed TFRecord format description see:
    https://www.tensorflow.org/versions/master/api_docs/python/python_io.html#tfrecords-format-details
  """

  def __init__(self, file_path_prefix, coder, file_name_suffix, num_shards,
               shard_name_template, compression_type):
    """Initialize a TFRecordSink. See WriteToTFRecord for details."""

    super(_TFRecordSink, self).__init__(
        file_path_prefix=file_path_prefix,
        coder=coder,
        file_name_suffix=file_name_suffix,
        num_shards=num_shards,
        shard_name_template=shard_name_template,
        mime_type='application/octet-stream',
        compression_type=compression_type)

  def write_encoded_record(self, file_handle, value):
    _TFRecordUtil.write_record(file_handle, value)


class WriteToTFRecord(beam.PTransform):
  """Transform for writing to TFRecord sinks."""

  def __init__(self,
               file_path_prefix,
               coder=coders.BytesCoder(),
               file_name_suffix='',
               num_shards=0,
               shard_name_template=filebasedsink.DEFAULT_SHARD_NAME_TEMPLATE,
               compression_type=CompressionTypes.AUTO,
               **kwargs):
    """Initialize WriteToTFRecord transform.

    Args:

      file_path_prefix: The file path to write to. The files written will begin
        with this prefix, followed by a shard identifier (see num_shards), and
        end in a common extension, if given by file_name_suffix. In most cases,
        only this argument is specified and num_shards, shard_name_template, and
        file_name_suffix use default values.
      coder: Coder used to encode each record.
      file_name_suffix: Suffix for the files written.
      num_shards: The number of files (shards) used for output. If not set, the
        service will decide on the optimal number of shards.
        Constraining the number of shards is likely to reduce
        the performance of a pipeline.  Setting this value is not recommended
        unless you require a specific number of output files.
      shard_name_template: A template string containing placeholders for
        the shard number and shard count. Currently only '' and
        '-SSSSS-of-NNNNN' are patterns accepted by the service.
        When constructing a filename for a particular shard number, the
        upper-case letters 'S' and 'N' are replaced with the 0-padded shard
        number and shard count respectively.  This argument can be '' in which
        case it behaves as if num_shards was set to 1 and only one file will be
        generated. The default pattern used is '-SSSSS-of-NNNNN'.
      compression_type: Used to handle compressed output files. Typical value
          is CompressionTypes.AUTO, in which case the file_path's extension will
          be used to detect the compression.
      **kwargs: Optional args dictionary. These are passed through to parent
        constructor.

    Returns:
      A WriteToTFRecord transform object.
    """
    super(WriteToTFRecord, self).__init__(**kwargs)
    self._args = (file_path_prefix, coder, file_name_suffix, num_shards,
                  shard_name_template, compression_type)

  # TODO(b/33677990): Remove apply method.
  def apply(self, pcoll):
    return self.expand(pcoll)

  def expand(self, pcoll):
    return pcoll | beam.io.Write(_TFRecordSink(*self._args))
