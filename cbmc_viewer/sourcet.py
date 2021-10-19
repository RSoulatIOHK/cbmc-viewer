# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The source files used to build a goto binary."""

import enum
import json
import logging
import os
import re
import subprocess

import voluptuous
import voluptuous.humanize

from cbmc_viewer import parse
from cbmc_viewer import runt
from cbmc_viewer import srcloct
from cbmc_viewer import symbol_table
from cbmc_viewer import util

JSON_TAG = 'viewer-source'

################################################################

class Sources(enum.Enum):
    """Methods for listing source files."""

    FIND = 1
    WALK = 2
    MAKE = 3
    GOTO = 4

################################################################
# Source validator

VALID_SOURCE = voluptuous.schema_builder.Schema({
    # Absolute path to the source root
    voluptuous.schema_builder.Required('root'): str,
    # Relative paths to source files under the source root
    'files': [str],
    # Absolute paths to source files including standard include files
    voluptuous.schema_builder.Required('all_files'): [str],
    # Statistics generated by sloc
    voluptuous.schema_builder.Optional('lines_of_code'): {str: int},
}, required=True)

################################################################

class Source:
    """Source files used to build a goto binary.

    There are several methods for discovering the list of source files
    used to build a goto binary.  Each method is implemented as a
    subclass of the source class.  A source object must be created as
    an object of one of these subclasses.  The subclass is responsible
    for defining the paths to the root and to the source files.  The
    class itself maintains these paths with a consistent representation.
    """

    def __init__(self, root=None, files=None, sloc=False):

        # Absolute path to the source root (initialized by subclass)
        self.root = srcloct.abspath(root) if root else ''

        # Absolute paths to all source files (initialized by subclass)
        files = files or []
        self.all_files = sorted({srcloct.abspath(path) for path in files})

        # Relative paths to those source files under the source root
        self.files = [srcloct.normpath(path[len(self.root)+1:])
                      for path in self.all_files
                      if path.startswith(self.root)]

        # Source code statistics generated by sloc
        if sloc:
            lines_of_code = self.sloc(self.files, self.root)
            if lines_of_code:
                self.lines_of_code = lines_of_code

        self.validate()

    def __repr__(self):
        """A dict representation of sources."""

        self.validate()
        return self.__dict__

    def __str__(self):
        """A string representation of sources."""

        return json.dumps({JSON_TAG: self.__repr__()}, indent=2, sort_keys=True)

    def validate(self, sources=None):
        """Validate sources."""

        return voluptuous.humanize.validate_with_humanized_errors(
            sources or self.__dict__, VALID_SOURCE
        )

    def dump(self, filename=None, directory=None):
        """Write sources to a file or stdout."""

        util.dump(self, filename, directory)

    @staticmethod
    def sloc(files, root):
        """Run sloc on a list of files under root."""

        loc = {}
        sources = files
        while sources:
            files = sources[:100]
            sources = sources[100:]

            logging.info('Running sloc on %s files starting with %s...',
                         len(files),
                         files[0])
            command = ['sloc', '-f', 'json'] + files
            logging.debug('sloc: %s', ' '.join(command))

            try:
                result = runt.run(command, root)
            except FileNotFoundError as error:
                # handle sloc command not found
                logging.info('sloc not found: %s', error.strerror)
                return None
            except OSError as error:
                # handle sloc argument list too long
                if error.errno != 7: # errno==7 is 'Argument list too long'
                    raise
                logging.info('sloc argument list too long: %s', error.strerror)
                return None
            except subprocess.CalledProcessError as error:
                # handle sloc error generated by running sloc
                logging.info('Unable to run sloc: %s', error.stderr)
                return None
            # sloc produces useful data in addition to the summary
            data = json.loads(result)['summary']
            for key, val in data.items():
                loc[key] = int(val) + loc.get(key, 0)

        return loc

################################################################

class SourceFromJson(Source):
    """Source files loaded from the output of make-source."""

    def __init__(self, source_jsons, sloc=False):
        """Read the list of source files from a set of json files.

        The argument 'source_jsons' is a list of json files containing the
        json serializations of Source objects. Confirm that these
        lists all have the same source root, and merge these lists of
        source files into a single list.  Don't run sloc on the list
        of source files, by default, since the list from find and walk
        can be very long.
        """

        if not source_jsons:
            raise UserWarning('No sources')

        def load(source_json):
            try:
                source = parse.parse_json_file(source_json)[JSON_TAG]
            except TypeError:
                raise UserWarning(
                    "Failed to load sources from {}".format(source_json)
                ) from None
            self.validate(source)
            return source

        def merge(sources):
            roots = list({source['root'] for source in sources})
            files = list({src
                          for source in sources
                          for src in source['all_files']})
            if len(roots) != 1:
                raise UserWarning(
                    'Source lists have different roots: {}'.format(roots)
                )
            return {'root': roots[0], 'files': files}

        source = merge([load(source_json) for source_json in source_jsons])
        super().__init__(source['root'], source['files'], sloc)

################################################################

class SourceFromGoto(Source):
    """Source files found in the symbol table of a goto binary."""

    def __init__(self, goto, wkdir, srcdir, sloc=False):
        """Read the list of source files from goto symbol tables."""

        if not goto:
            raise UserWarning('No goto program')

        files = sorted(symbol_table.source_files(goto, wkdir))
        super().__init__(srcdir, files, sloc)

################################################################

class SourceFromFind(Source):
    """Source files found with find from the source root.

    Using find is faster than using walk, but find may not exist on
    some platforms like Windows.  This method of listing source files
    may include files in the source tree that were not used to build
    the goto binary.  It may also omit many files like system include
    files that were needed to build the goto binary.
    """

    def __init__(self, root, exclude=None, extensions=None, sloc=False):
        """Use find to list the source files under root.

        Don't  run sloc on the list of files, by default, since sloc
        can be slow on long lists, and find can generate long lists.
        """

        files = self.find_sources(root, exclude, extensions)
        super().__init__(root, files, sloc)

    @staticmethod
    def find_sources(root, exclude, extensions):
        """Use find to list the source files under root."""

        logging.info('Running find...')
        cmd = ['find', '-L', '.']
        files = runt.run(cmd, root).strip().splitlines()
        logging.info('Running find...done')
        return select_source_files(files, root, exclude, extensions)

################################################################

class SourceFromWalk(Source):
    """Source files found with walk from the source root.

    Using walk is slower than using find, but walk exists on all
    platforms including Windows.  This method of listing source files
    may include files in the source tree that were not used to build
    the goto binary.  It may also omit many files like system include
    files that were needed to build the goto binary.
    """

    def __init__(self, root, exclude=None, extensions=None, sloc=False):
        """Use walk to list the source files under root."""

        files = self.find_sources(root, exclude, extensions)
        super().__init__(root, files, sloc)

    @staticmethod
    def find_sources(root, exclude=None, extensions=None):
        """Use walk to list the source files under root.

        Don't run sloc on the list of files, by default, since sloc
        can be slow on long lists, and walk can generate long lists.
        """

        logging.info('Running walk...')
        files = []
        for path, _, filenames in os.walk(root, followlinks=True):
            names = [os.path.join(path, name) for name in filenames
                     if name.lower().endswith(('.h', '.c', '.inl'))]
            files.extend(names)
        logging.info('Running walk...done')
        return select_source_files(files, root, exclude, extensions)

################################################################

class SourceFromMake(Source):
    """Source files use by preprocessor to build the goto binary.

    This method yields the most accurate list of files needed to build
    the goto binary.  This method assumes that 'make GOTO_CC=goto-cc goto'
    will build the goto binary.  It works by running
    'make "GOTO_CC=goto-cc -E" goto' to build the binary with the
    preprocessor.  The preprocessed output will include line markers
    that name the files used by the preprocessor.  This list of files
    is returned as the list of source files needed to build the
    goto binary.

    This method has the disadvantage of modifing the object files.  In
    particular, running the preprocessor replaces the object files
    with text files containing the preprocessor output.  To avoid
    confusing compilers and cbmc itself, this method first runs 'make
    clean' to remove object files, then runs the preprocessor, then
    runs 'make clean' to remove the preprocessed output.  Using this
    method will require rebuilding the goto binary before running
    cbmc.
    """

    # TODO: run preprocessor without destroying existing goto binary
    # But how do we use make to run the preprocessor without removing the
    # target and dependencies that drive make?

    def __init__(self, root, build, sloc=True):
        """Use make to list the source files.

        The argument build is the directory in which that make command
        should be run, and root is the source tree root.
        """

        files = self.find_sources(build)
        super().__init__(root, files, sloc)

    def find_sources(self, build):
        """Use make to list the source files used to build a goto binary."""

        # Remove object files
        runt.run(['make', 'clean'], build)

        # Build with the preprocessor
        preprocessor_commands = self.build_with_preprocessor(build)
        logging.debug('preprocessor commands: %s', preprocessor_commands)
        preprocessed_filenames = self.extract_filenames(preprocessor_commands,
                                                        build)
        logging.debug('preprocessed filenames: %s', preprocessed_filenames)
        preprocessed_output = self.read_output(preprocessed_filenames)
        #logging.debug('preprocessed output: %s', preprocessed_output)
        source_files = self.extract_source_filenames(preprocessed_output, build)
        logging.debug('source files: %s', source_files)

        # Remove preprocessor output
        runt.run(['make', 'clean'], build)

        return source_files

    @staticmethod
    def build_with_preprocessor(build):
        """Build the goto binary using goto-cc as a preprocessor.

        Return the list of goto-cc commands used in the build.
        """

        # Make will fail when it tries to link the preprocessed output
        # What is a system-independent way of skipping the link failure?
        # For now, we assume error code 2 is generated by the link failure.

        # build the project with the preprocessor and capture the make output
        result = runt.run(['make', 'GOTO_CC=goto-cc -E', 'goto'], build,
                          ignored=[2])

        # strip line continuations in the make output
        result = result.replace('\\\n', '')

        # return the invocations of goto-cc in the make output
        return [line.strip()
                for line in result.splitlines()
                if line.strip().startswith('goto-cc')]

    @staticmethod
    def extract_filenames(commands, build):
        """Return the names of the files containing the preprocessor output.

        The argument commands is the list of goto-cc invocations in
        the make output.  The argument build is the directory in which
        make was invoked.

        Assume that each invocation of goto-cc uses -o OUTPUT to name
        the file containing the preprocessed output.  Assume that if
        OUTPUT is not an absolute path, then it is a path relative to
        the build directory.
        """

        files = []
        for cmd in commands:
            match = re.search(r' -o (\S+) ', cmd)
            if match:
                name = match.group(1)
                name = os.path.join(build, name)
                files.append(os.path.abspath(name))
        return files

    @staticmethod
    def read_output(files):
        """Return the preprocessor output as a list of lines."""

        output = []
        for name in files:
            try:
                with open(name) as handle:
                    output.extend(handle.read().splitlines())
            except FileNotFoundError:
                # The output file for the failed linking step will be in list
                logging.debug("Can't open '%s', "
                              'probably due to the failure of the link step',
                              name)
        return output

    @staticmethod
    def extract_source_filenames(output, build):
        """Return the list of source files in the preprocessor output.

        The argument output is the preprocesor output given as a list
        of lines.  The argument build is the directory in
        which make was invoked.

        Assume that if a source file is not an absolute path, then it
        is a path relative to the build directory.
        """

        # extract linemarkers from preprocessor output
        # NOTE:
        #   linemarkers have form '# linenum "filename" flags' (space after #)
        #   directives have form '#directive' (no space after #)
        linemarkers = [line for line in output if line.strip().startswith('# ')]

        # extract filenames from linemarkers
        filenames = [re.search(r'"(.*)"', line).group(1)
                     for line in linemarkers]

        # skip filenames generated by the preprocessor
        # examples of preprocessor output are
        #  # 1 "/usr/project/proof/<built-in>" 3
        #  # 1 "/usr/project/proof/<command-line>" 1
        #  # 1 "/usr/project/proof/<command line>" 1
        filenames = [name for name in filenames
                     if os.path.basename(name) not in
                     ['<built-in>',
                      '<command-line>',
                      '<command line>']]
        filenames = [os.path.join(build, name) for name in filenames]
        filenames = [os.path.normpath(name) for name in filenames]
        return sorted(set(filenames))

################################################################

def select_source_files(files, root, exclude=None, extensions=None):
    """Return source files from a list of files under root.

    Return absolute paths to the source files in a list of files.
    Files in the list are paths relative to root.  Exclude from the
    list files matching the regular expression 'exclude'.  Select from
    the list files with file extensions matching the regular
    expression 'extensions'.
    """

    files = [os.path.normpath(path) for path in files]
    if exclude is not None:
        files = [path for path in files
                 if not re.match(exclude, path, re.I)]
    if extensions is not None:
        files = [path for path in files
                 if re.match(extensions, os.path.splitext(path)[1], re.I)]
    files = [os.path.join(root, path) for path in files]
    return files

################################################################
# make-source

# pylint: disable=inconsistent-return-statements

def fail(msg):
    """Log failure and raise exception."""

    logging.info(msg)
    raise UserWarning(msg)

def make_source(viewer_source, goto, source_method, srcdir,
                wkdir, exclude, extensions):
    """The implementation of make-source."""

    wkdir = srcloct.abspath(wkdir) if wkdir else None
    srcdir = srcloct.abspath(srcdir) if srcdir else None

    if viewer_source:
        logging.info("Sources by SourceFromJson")
        return SourceFromJson(viewer_source)

    # source_method was set to a reasonable value by optionst.defaults()
    # if it was not specified on the command line.

    if source_method == Sources.GOTO:
        if goto is None or wkdir is None or srcdir is None:
            fail("make-source: expected --goto and --srcdir and --wkdir")
        logging.info("Sources by SourceFromGoto")
        return SourceFromGoto(goto, wkdir, srcdir)

    if source_method == Sources.MAKE:
        if srcdir is None or wkdir is None:
            fail("make-source: expected --srcdir and --wkdir")
        logging.info("Sources by SourceFromMake")
        return SourceFromMake(srcdir, wkdir)

    if source_method == Sources.FIND:
        if srcdir is None:
            fail("make-source: expected --srcdir")
        logging.info("Sources by SourceFromFind")
        return SourceFromFind(srcdir, exclude, extensions)

    if source_method == Sources.WALK:
        if srcdir is None:
            fail("make-source: expected --srcdir")
        logging.info("Sources by SourceFromWalk")
        return SourceFromWalk(srcdir, exclude, extensions)

    logging.info("make-source: nothing to do")
    return Source()

################################################################
