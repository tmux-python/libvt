"""Sphinx configuration for libvt."""

from __future__ import annotations

import pathlib
import sys

from gp_sphinx.config import make_linkcode_resolve, merge_sphinx_config
from gp_sphinx.defaults import DEFAULT_SPHINX_FONT_PRELOAD

import libvt

cwd = pathlib.Path(__file__).parent
project_root = cwd.parent
project_src = project_root / "src"

sys.path.insert(0, str(project_src))

about: dict[str, str] = {}
with (project_src / "libvt" / "__about__.py").open(encoding="utf-8") as fp:
    exec(fp.read(), about)

conf = merge_sphinx_config(
    project=about["__title__"],
    version=about["__version__"],
    copyright=about["__copyright__"],
    source_repository=f"{about['__github__']}/",
    docs_url=about["__docs__"],
    source_branch="master",
    light_logo="img/libvt.svg",
    dark_logo="img/libvt.svg",
    extra_extensions=[
        "sphinx_autodoc_api_style",
        "sphinx_autodoc_pytest_fixtures",
        "sphinx.ext.todo",
    ],
    intersphinx_mapping={
        "python": ("https://docs.python.org/3/", None),
        "pytest": ("https://docs.pytest.org/en/stable/", None),
    },
    linkcode_resolve=make_linkcode_resolve(libvt, about["__github__"], src_dir="src"),
    theme_options={
        "announcement": (
            "<em>Friendly reminder:</em> Pin the package — libvt is"
            " pre-1.0 and APIs may <a href='/migration/'>change</a>"
            " before 1.0."
        ),
    },
    html_favicon="_static/img/libvt.svg",
    html_css_files=["css/libvt.css"],
    sphinx_font_preload=[
        *DEFAULT_SPHINX_FONT_PRELOAD,
        ("IBM Plex Mono", 600, "normal"),
    ],
    html_extra_path=["manifest.json"],
    rediraffe_redirects="redirects.txt",
    exclude_patterns=[
        "_build",
        "AGENTS.md",
        "CLAUDE.md",
        "project/adr/_template.md",
    ],
)
conf.setdefault("autodoc_default_options", {})["private-members"] = False
globals().update(conf)
