import datetime
import os
import os as _os
import pathlib
import typing
from abc import abstractmethod
from distutils import dir_util as _dir_util
from shutil import copyfile as _copyfile
from typing import Union, Dict
from uuid import UUID

from flytekit.loggers import logger
from flytekit.common.exceptions.user import FlyteAssertion
from flytekit.common.utils import PerformanceTimer
from flytekit.interfaces.random import random


class UnsupportedPersistenceOp(Exception):
    """
    This exception is raised for all methods when a method is not supported by the data persistence layer
    """

    def __init__(self, message: str):
        super(UnsupportedPersistenceOp, self).__init__(message)


class DataPersistence(object):
    """
    Base abstract type for all  DataPersistence operations. This can be plugged in using the flytekitplugins architecture
    """

    def __init__(self, name: str, *args, **kwargs):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def listdir(self, path: str, recursive: bool = False) -> typing.Generator[str, None, None]:
        """
        Returns true if the given path exists, else false
        """
        raise UnsupportedPersistenceOp(f"Listing a directory is not supported by the persistence plugin {self.name}")

    @abstractmethod
    def exists(self, path: str) -> bool:
        """
        Returns true if the given path exists, else false
        """
        pass

    @abstractmethod
    def download_directory(self, remote_path: str, local_path: str):
        """
        downloads a directory from path to path recursively
        """
        pass

    @abstractmethod
    def download(self, remote_path: str, local_path: str):
        """
        downloads a file from path to path
        """
        pass

    @abstractmethod
    def upload(self, file_path: str, to_path: str):
        """
        uploads the given file to path
        """
        pass

    @abstractmethod
    def upload_directory(self, local_path: str, remote_path: str):
        """
        uploads a directory from path to path recursively
        """
        pass

    @abstractmethod
    def construct_path(self, add_protocol: bool, *paths) -> str:
        """
        if add_protocol is true then <protocol> is prefixed else
        Constructs a path in the format <base><delim>*args
        delim is dependent on the storage medium.
        each of the args is joined with the delim
        """
        pass


class DataPersistencePlugins(object):
    """
    This is core plugin engine that stores all DataPersistence plugins. To add a new plugin use

    .. code-block:: python

       DataPersistencePlugins.register_plugin("s3:/", DataPersistence(), force=True|False)

    These plugins should always be registered. Follow the plugin registration guidelines to auto-discover your plugins.
    """
    _PLUGINS: Dict[str, DataPersistence] = {}

    @classmethod
    def register_plugin(cls, protocol: str, plugin: DataPersistence, force: bool = False):
        """
        Registers the supplied plugin for the specified protocol if one does not already exists.
        If one exists and force is default or False, then a TypeError is raised.
        If one does not exist then it is registered
        If one exists, but force == True then the existing plugin is overriden
        """
        if protocol in cls._PLUGINS:
            p = cls._PLUGINS[protocol]
            if p == plugin:
                return
            if not force:
                raise TypeError(
                    f"Cannot register plugin {plugin.name} for protocol {protocol} as plugin {p.name} is already"
                    f" registered for the same protocol. You can force register the new plugin by passing force=True")

        cls._PLUGINS[protocol] = plugin

    @classmethod
    def find_plugin(cls, path: str) -> DataPersistence:
        """
        Returns a plugin for the given protocol, else raise a TypeError
        """
        for k, p in cls._PLUGINS.items():
            if path.startswith(k):
                return p
        raise TypeError(f"No plugin found for matching protocol of path {path}")

    @classmethod
    def print_all_plugins(cls):
        """
        Prints all the plugins and their associated protocoles
        """
        for k, p in cls._PLUGINS.items():
            print(f"Plugin {p.name} registered for protocol {k}")

    @classmethod
    def is_supported_protocol(cls, protocol: str) -> bool:
        """
        Returns true if the given protocol is has a registered plugin for it
        """
        return protocol in cls._PLUGINS


class DiskPersistence(DataPersistence):
    PROTOCOL = "file://"

    def __init__(self, *args, **kwargs):
        """
        :param Text sandbox:
        """
        super().__init__(name="local", *args, **kwargs)

    @staticmethod
    def _make_local_path(path):
        if not _os.path.exists(path):
            try:
                _os.makedirs(path)
            except OSError:  # Guard against race condition
                if not _os.path.isdir(path):
                    raise

    @staticmethod
    def strip_file_header(path: str) -> str:
        """
        Drops file:// if it exists from the file
        """
        if path.startswith("file://"):
            return path.replace("file://", "", 1)
        return path

    def listdir(self, path: str, recursive: bool = False) -> typing.Generator[str, None, None]:
        if not recursive:
            files = os.listdir(self.strip_file_header(path))
            for f in files:
                yield f
            return

        for root, subdirs, files in os.walk(self.strip_file_header(path)):
            for f in files:
                yield os.path.join(root, f)
        return

    def exists(self, path: str):
        return _os.path.exists(self.strip_file_header(path))

    def download_directory(self, from_path: str, to_path: str):
        if from_path != to_path:
            _dir_util.copy_tree(self.strip_file_header(from_path), self.strip_file_header(to_path))

    def download(self, from_path: str, to_path: str):
        _copyfile(self.strip_file_header(from_path), self.strip_file_header(to_path))

    def upload(self, from_path: str, to_path: str):
        # Emulate s3's flat storage by automatically creating directory path
        self._make_local_path(_os.path.dirname(self.strip_file_header(to_path)))
        # Write the object to a local file in the sandbox
        _copyfile(self.strip_file_header(from_path), self.strip_file_header(to_path))

    def upload_directory(self, from_path, to_path):
        self.download_directory(from_path, to_path)

    def construct_path(self, add_protocol: bool, *args) -> str:
        if add_protocol:
            return os.path.join(self.PROTOCOL, *args)
        return os.path.join(*args)


class FileAccessProvider(object):
    def __init__(self, local_sandbox_dir: Union[str, os.PathLike], raw_output_prefix: str):
        # Local access
        if local_sandbox_dir is None or local_sandbox_dir == "":
            raise Exception("Can't use empty path")
        local_sandbox_dir_appended = os.path.join(local_sandbox_dir, "local_flytekit")
        self._local_sandbox_dir = pathlib.Path(local_sandbox_dir_appended)
        self._local_sandbox_dir.mkdir(parents=True, exist_ok=True)
        self._local = DiskPersistence()

        self._default_remote = DataPersistencePlugins.find_plugin(raw_output_prefix)
        self._raw_output_prefix = raw_output_prefix

    @staticmethod
    def is_remote(path: Union[str, os.PathLike]) -> bool:
        """
        Deprecated. Lets find a replacement
        """
        if path.startswith("/") or path.startswith("file://"):
            return False
        return True

    @property
    def local_sandbox_dir(self) -> os.PathLike:
        return self._local_sandbox_dir

    @property
    def local_access(self) -> DiskPersistence:
        return self._local

    def construct_random_path(self, persist: DataPersistence,
                              file_path_or_file_name: typing.Optional[str] = None) -> str:
        """
        Use file_path_or_file_name, when you want a random directory, but want to preserve the leaf file name
        """
        key = UUID(int=random.random.getrandbits(128)).hex
        if file_path_or_file_name:
            _, tail = os.path.split(file_path_or_file_name)
            if tail:
                return persist.construct_path(False, self._raw_output_prefix, key, tail)
            else:
                logger.warning(f"No filename detected in {file_path_or_file_name}, generating random path")
        return persist.construct_path(False, self._raw_output_prefix, key)

    def get_random_remote_path(self, file_path_or_file_name: typing.Optional[str] = None) -> str:
        """
        Constructs a randomized path on the configured raw_output_prefix (persistence layer). the random bit is a UUID
        and allows for disambiguating paths within the same directory.

        Use file_path_or_file_name, when you want a random directory, but want to preserve the leaf file name
        """
        return self.construct_random_path(self._default_remote, file_path_or_file_name)

    def get_random_remote_directory(self):
        return self.get_random_remote_path(None)

    def get_random_local_path(self, file_path_or_file_name: typing.Optional[str] = None) -> str:
        """
        Use file_path_or_file_name, when you want a random directory, but want to preserve the leaf file name
        """
        return self.construct_random_path(self._local, file_path_or_file_name)

    def get_random_local_directory(self) -> str:
        _dir = self.get_random_local_path(None)
        pathlib.Path(_dir).mkdir(parents=True, exist_ok=True)
        return _dir

    def exists(self, path: str) -> bool:
        """
        checks if the given path exists
        """
        return DataPersistencePlugins.find_plugin(path).exists(path)

    def download_directory(self, remote_path: str, local_path: str):
        """
        Downloads directory from given remote to local path
        """
        return DataPersistencePlugins.find_plugin(remote_path).download_directory(remote_path, local_path)

    def download(self, remote_path: str, local_path: str):
        """
        Downloads from remote to local
        """
        return DataPersistencePlugins.find_plugin(remote_path).download(remote_path, local_path)

    def upload(self, file_path: str, to_path: str):
        """
        :param Text file_path:
        :param Text to_path:
        """
        return DataPersistencePlugins.find_plugin(to_path).upload(file_path, to_path)

    def upload_directory(self, local_path: str, remote_path: str):
        """
        :param Text local_path:
        :param Text remote_path:
        """
        # TODO: https://github.com/flyteorg/flyte/issues/762 - test if this works!
        return DataPersistencePlugins.find_plugin(remote_path).upload_directory(local_path, remote_path)

    def get_data(self, remote_path: str, local_path: str, is_multipart=False):
        """
        :param Text remote_path:
        :param Text local_path:
        :param bool is_multipart:
        """
        try:
            with PerformanceTimer("Copying ({} -> {})".format(remote_path, local_path)):
                if is_multipart:
                    self.download_directory(remote_path, local_path)
                else:
                    self.download(remote_path, local_path)
        except Exception as ex:
            raise FlyteAssertion(
                "Failed to get data from {remote_path} to {local_path} (recursive={is_multipart}).\n\n"
                "Original exception: {error_string}".format(
                    remote_path=remote_path,
                    local_path=local_path,
                    is_multipart=is_multipart,
                    error_string=str(ex),
                )
            )

    def put_data(self, local_path: Union[str, os.PathLike], remote_path: str, is_multipart=False):
        """
        The implication here is that we're always going to put data to the remote location, so we .remote to ensure
        we don't use the true local proxy if the remote path is a file://

        :param Text local_path:
        :param Text remote_path:
        :param bool is_multipart:
        """
        try:
            with PerformanceTimer("Writing ({} -> {})".format(local_path, remote_path)):
                if is_multipart:
                    self._default_remote.upload_directory(local_path, remote_path)
                else:
                    self._default_remote.upload(local_path, remote_path)
        except Exception as ex:
            raise FlyteAssertion(
                f"Failed to put data from {local_path} to {remote_path} (recursive={is_multipart}).\n\n"
                f"Original exception: {str(ex)}"
            ) from ex


DataPersistencePlugins.register_plugin("file://", DiskPersistence())
DataPersistencePlugins.register_plugin("/", DiskPersistence())

# TODO make this use tmpdir
tmp_dir = os.path.join("/tmp/flyte", datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
default_local_file_access_provider = FileAccessProvider(
    local_sandbox_dir=os.path.join(tmp_dir, "sandbox"),
    raw_output_prefix=os.path.join(tmp_dir, "raw")
)