from projspec.proj.base import ProjectSpec
from projspec.utils import AttrDict


class UVProject(ProjectSpec):
    """UV-runnable project

    Note: uv can run any python project, but this tests for uv-specific
    config. See also ``projspec.deploty.python.UVRunner``.
    """

    def match(self):
        contents = self.root.filelist
        basenames = {_.rsplit("/", 1)[-1]: _ for _ in contents}
        if "uv.lock" in basenames or "uv.toml" in basenames or ".python-version" in basenames:
            return True
        if "uv" in self.root.pyproject.get("tools", {}):
            return True
        if self.root.pyproject.get("build-system", {}).get("build-backend", "") == "uv_build":
            return True
        return False

    @property
    def contents(self) -> AttrDict:
        # environment spec
        # commands
        # python package (unless tools.uv.package == False)
        return AttrDict()

    @property
    def artifacts(self) -> AttrDict:
        # lockfile
        # runtime environment
        # process from defined commands
        return AttrDict()
