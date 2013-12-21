import re
import binwalk.core.module
from binwalk.core.compat import *
from binwalk.core.common import str2int, get_quoted_strings, MathExpression

class SmartSignature:
	'''
	Class for parsing smart signature tags in libmagic result strings.

	This class is intended for internal use only, but a list of supported 'smart keywords' that may be used 
	in magic files is available via the SmartSignature.KEYWORDS dictionary:

		from binwalk import SmartSignature

		for (i, keyword) in SmartSignature().KEYWORDS.iteritems():
			print keyword
	'''

	KEYWORD_DELIM_START = "{"
	KEYWORD_DELIM_END = "}"
	KEYWORDS = {
		'jump'					: '%sjump-to-offset:' % KEYWORD_DELIM_START,
		'filename'				: '%sfile-name:' % KEYWORD_DELIM_START,
		'filesize'				: '%sfile-size:' % KEYWORD_DELIM_START,
		'raw-string'			: '%sraw-string:' % KEYWORD_DELIM_START,	# This one is special and must come last in a signature block
		'string-len'			: '%sstring-len:' % KEYWORD_DELIM_START,	# This one is special and must come last in a signature block
		'raw-size'				: '%sraw-string-length:' % KEYWORD_DELIM_START,
		'adjust'				: '%soffset-adjust:' % KEYWORD_DELIM_START,
		'delay'					: '%sextract-delay:' % KEYWORD_DELIM_START,
		'year'					: '%sfile-year:' % KEYWORD_DELIM_START,
		'epoch'					: '%sfile-epoch:' % KEYWORD_DELIM_START,
		'math'					: '%smath:' % KEYWORD_DELIM_START,

		'raw-replace'			: '%sraw-replace%s' % (KEYWORD_DELIM_START, KEYWORD_DELIM_END),
		'one-of-many'			: '%sone-of-many%s' % (KEYWORD_DELIM_START, KEYWORD_DELIM_END),
		'string-len-replace'	: '%sstring-len%s' % (KEYWORD_DELIM_START, KEYWORD_DELIM_END),
	}

	def __init__(self, filter, ignore_smart_signatures=False):
		'''
		Class constructor.

		@filter                  - Instance of the MagicFilter class.
		@ignore_smart_signatures - Set to True to ignore smart signature keywords.

		Returns None.
		'''
		self.filter = filter
		self.valid = True
		self.last_one_of_many = None
		self.ignore_smart_signatures = ignore_smart_signatures

	def parse(self, data):
		'''
		Parse a given data string for smart signature keywords. If any are found, interpret them and strip them.

		@data - String to parse, as returned by libmagic.

		Returns a dictionary of parsed values.
		'''
		results = {
			'offset'		: '',		# Offset where the match was found, filled in by Binwalk.single_scan.
			'description'	: '',		# The libmagic data string, stripped of all keywords
			'name'			: '',		# The original name of the file, if known
			'delay'			: '',		# Extract delay description
			'extract'		: '',		# Name of the extracted file, filled in by Binwalk.single_scan.
			'jump'			: 0,		# The relative offset to resume the scan from
			'size'			: 0,		# The size of the file, if known
			'adjust'		: 0,		# The relative offset to add to the reported offset
			'year'			: 0,		# The file's creation/modification year, if reported in the signature
			'epoch'			: 0,		# The file's creation/modification epoch time, if reported in the signature
			'valid'			: True,		# Set to False if parsed numerical values appear invalid
		}

		self.valid = True

		# If smart signatures are disabled, or the result data is not valid (i.e., potentially malicious), 
		# don't parse anything, just return the raw data as the description.
		if self.ignore_smart_signatures or not self._is_valid(data):
			results['description'] = data
		else:
			# Calculate and replace special keywords/values
			data = self._replace_maths(data)
			data = self._parse_raw_strings(data)
			data = self._parse_string_len(data)

			# Parse the offset-adjust value. This is used to adjust the reported offset at which 
			# a signature was located due to the fact that MagicParser.match expects all signatures
			# to be located at offset 0, which some wil not be.
			results['adjust'] = self._get_math_arg(data, 'adjust')

			# Parse the file-size value. This is used to determine how many bytes should be extracted
			# when extraction is enabled. If not specified, everything to the end of the file will be
			# extracted (see Binwalk.scan).
			try:
				results['size'] = str2int(self._get_math_arg(data, 'filesize'))
			except KeyboardInterrupt as e:
				raise e
			except Exception:
				pass

			try:
				results['year'] = str2int(self._get_keyword_arg(data, 'year'))
			except KeyboardInterrupt as e:
				raise e
			except Exception:
				pass
			
			try:
				results['epoch'] = str2int(self._get_keyword_arg(data, 'epoch'))
			except KeyboardInterrupt as e:
				raise e
			except Exception:
				pass

			results['delay'] = self._get_keyword_arg(data, 'delay')

			# Parse the string for the jump-to-offset keyword.
			# This keyword is honored, even if this string result is one of many.
			results['jump'] = self._get_math_arg(data, 'jump')

			# If this is one of many, don't do anything and leave description as a blank string.
			# Else, strip all keyword tags from the string and process additional keywords as necessary.
			if not self._one_of_many(data):
				results['name'] = self._get_keyword_arg(data, 'filename').strip('"')
				results['description'] = self._strip_tags(data)

		results['valid'] = self.valid

		return binwalk.core.module.Result(**results)

	def _is_valid(self, data):
		'''
		Validates that result data does not contain smart keywords in file-supplied strings.

		@data - Data string to validate.

		Returns True if data is OK.
		Returns False if data is not OK.
		'''
		# All strings printed from the target file should be placed in strings, else there is
		# no way to distinguish between intended keywords and unintended keywords. Get all the
		# quoted strings.
		quoted_data = get_quoted_strings(data)

		# Check to see if there was any quoted data, and if so, if it contained the keyword starting delimiter
		if quoted_data and self.KEYWORD_DELIM_START in quoted_data:
			# If so, check to see if the quoted data contains any of our keywords.
			# If any keywords are found inside of quoted data, consider the keywords invalid.
			for (name, keyword) in iterator(self.KEYWORDS):
				if keyword in quoted_data:
					return False
		return True

	def _one_of_many(self, data):
		'''
		Determines if a given data string is one result of many.

		@data - String result data.

		Returns True if the string result is one of many.
		Returns False if the string result is not one of many.
		'''
		if self.filter.valid_result(data):
			if self.last_one_of_many is not None and data.startswith(self.last_one_of_many):
				return True
		
			if self.KEYWORDS['one-of-many'] in data:
				# Only match on the data before the first comma, as that is typically unique and static
				self.last_one_of_many = data.split(',')[0]
			else:
				self.last_one_of_many = None
			
		return False

	def _get_keyword_arg(self, data, keyword):
		'''
		Retrieves the argument for keywords that specify arguments.

		@data    - String result data, as returned by libmagic.
		@keyword - Keyword index in KEYWORDS.

		Returns the argument string value on success.
		Returns a blank string on failure.
		'''
		arg = ''

		if has_key(self.KEYWORDS, keyword) and self.KEYWORDS[keyword] in data:
			arg = data.split(self.KEYWORDS[keyword])[1].split(self.KEYWORD_DELIM_END)[0]
			
		return arg

	def _get_math_arg(self, data, keyword):
		'''
		Retrieves the argument for keywords that specifiy mathematical expressions as arguments.

		@data    - String result data, as returned by libmagic.
		@keyword - Keyword index in KEYWORDS.

		Returns the resulting calculated value.
		'''
		value = 0

		arg = self._get_keyword_arg(data, keyword)
		if arg:
			value = MathExpression(arg).value
			if value is None:
				value = 0
				self.valid = False

		return value

	def _jump(self, data):
		'''
		Obtains the jump-to-offset value of a signature, if any.

		@data - String result data.

		Returns the offset to jump to.
		'''
		offset = 0

		offset_str = self._get_keyword_arg(data, 'jump')
		if offset_str:
			try:
				offset = str2int(offset_str)
			except KeyboardInterrupt as e:
				raise e
			except Exception:
				pass

		return offset

	def _replace_maths(self, data):
		'''
		Replace math keywords with the requested values.
			
		@data - String result data.

		Returns the modified string result data.
		'''
		while self.KEYWORDS['math'] in data:
			arg = self._get_keyword_arg(data, 'math')
			v = '%s%s%s' % (self.KEYWORDS['math'], arg, self.KEYWORD_DELIM_END)
			math_value = "%d" % self._get_math_arg(data, 'math')
			data = data.replace(v, math_value)

		return data

	def _parse_raw_strings(self, data):
		'''
		Process strings that aren't NULL byte terminated, but for which we know the string length.
		This should be called prior to any other smart parsing functions.

		@data - String to parse.

		Returns a parsed string.
		'''
		if not self.ignore_smart_signatures and self._is_valid(data):
			# Get the raw string  keyword arg
			raw_string = self._get_keyword_arg(data, 'raw-string')

			# Was a raw string  keyword specified?
			if raw_string:
				# Get the raw string length arg
				raw_size = self._get_keyword_arg(data, 'raw-size')
	
				# Is the raw string  length arg is a numeric value?
				if re.match('^-?[0-9]+$', raw_size):
					# Replace all instances of raw-replace in data with raw_string[:raw_size]
					# Also strip out everything after the raw-string keyword, including the keyword itself.
					# Failure to do so may (will) result in non-printable characters and this string will be 
					# marked as invalid when it shouldn't be.
					data = data[:data.find(self.KEYWORDS['raw-string'])].replace(self.KEYWORDS['raw-replace'], '"' + raw_string[:str2int(raw_size)] + '"')
		return data
		
	def _parse_string_len(self, data):
		'''
		Process {string-len} macros. 

		@data - String to parse.

		Returns parsed data string.
		'''
		if not self.ignore_smart_signatures and self._is_valid(data):

			# Get the raw string  keyword arg
			raw_string = self._get_keyword_arg(data, 'string-len')

			# Was a string-len  keyword specified?
			if raw_string:
				# Convert the string to an integer as a sanity check
				try:
					string_length = '%d' % len(raw_string)
				except KeyboardInterrupt as e:
					raise e
				except Exception:
					string_length = '0'

				# Strip out *everything* after the string-len keyword, including the keyword itself.
				# Failure to do so can potentially allow keyword injection from a maliciously created file.
				data = data.split(self.KEYWORDS['string-len'])[0].replace(self.KEYWORDS['string-len-replace'], string_length)
		return data

	def _strip_tags(self, data):
		'''
		Strips the smart tags from a result string.

		@data - String result data.

		Returns a sanitized string.
		'''
		if not self.ignore_smart_signatures:
			for (name, keyword) in iterator(self.KEYWORDS):
				start = data.find(keyword)
				if start != -1:
					end = data[start:].find(self.KEYWORD_DELIM_END)
					if end != -1:
						data = data.replace(data[start:start+end+1], "")
		return data

