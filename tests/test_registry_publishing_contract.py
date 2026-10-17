"""Distribution identity and published-release selection contract.

These are source-level checks; only a real Registry publishing run can prove
that the repository secret has the required permissions.
"""

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_registry_identity_is_maintainer_owned_and_distinct_from_upstream():
    conf = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert conf["project"]["name"] == "comfyui-h3-continuum-plus"
    assert conf["tool"]["comfy"]["PublisherId"] == "xmarre"
    assert conf["project"]["urls"]["Repository"] == (
        "https://github.com/xmarre/ComfyUI-H3-Continuum-Plus"
    )
    assert {"xmarre", "ukr8b3g-cmyk"} <= {
        author["name"] for author in conf["project"]["authors"]
    }


def test_publisher_only_uses_current_published_tag_and_registry_secret():
    text = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    assert "workflows: [release]" in text
    assert "workflow_dispatch:" in text
    assert 'gh release view "${tag}"' in text
    assert 'if [[ "${tag_sha}" != "${main_sha}" ]]' in text
    assert "ref: ${{ steps.release.outputs.tag }}" in text
    assert 'p["project"]["name"] != "comfyui-h3-continuum-plus"' in text
    assert 'p["tool"]["comfy"]["PublisherId"] != "xmarre"' in text
    assert 'if not os.environ.get("REGISTRY_ACCESS_TOKEN")' in text
    assert "vars.H3_CONTINUUM_REGISTRY_PUBLISH_ENABLED" not in text
    assert "vars.H3_CONTINUUM_AUTHORIZED_PUBLISHER_ID" not in text
