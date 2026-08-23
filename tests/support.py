"""Shared test doubles for the sandbox container."""

import json
from unittest.mock import MagicMock


def sandbox_container_mock(exec_results=None, model="test/model"):
    """A container mock that answers the calls the real pipeline makes.

    ``stage_file_in_container`` reads each staged file's size back with
    ``stat``, so a mock that returns one canned value for every ``exec_run``
    fails verification. This dispatches on the command instead, tracking the
    sizes actually uploaded via ``put_archive``.
    """
    container = MagicMock()
    container.short_id = "abc123"
    staged_sizes = {}

    def _put_archive(directory, archive):
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                staged_sizes[f"{directory.rstrip('/')}/{member.name}"] = member.size
        return True

    def _exec_run(cmd=None, **kwargs):
        if cmd and cmd[0] == "stat":
            path = cmd[-1]
            if path not in staged_sizes:
                return 1, (b"", b"No such file or directory")
            return 0, (str(staged_sizes[path]).encode(), b"")
        return 0, (json.dumps({"model": model}).encode(), b"")

    container.put_archive.side_effect = _put_archive
    container.exec_run.side_effect = _exec_run

    if exec_results is not None:
        api = container.client.api
        api.exec_create.return_value = {"Id": "exec-1"}
        api.exec_inspect.side_effect = list(exec_results)

    container.staged_sizes = staged_sizes
    return container


def staged_files(container):
    """Every path/content pair uploaded to *container* via ``put_archive``."""
    import io
    import tarfile

    staged = {}
    for call in container.put_archive.call_args_list:
        directory, archive = call.args
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                path = f"{directory.rstrip('/')}/{member.name}"
                staged[path] = tar.extractfile(member).read().decode("utf-8")
    return staged
