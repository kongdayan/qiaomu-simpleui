"""Regressions: pre-release update discovery + onboarding preset picker.

Run: python3 -m pip install lupa && python3 tests/test_updater_and_onboarding_presets.py

Two shipped bugs are pinned here:

1. The updater asked GitHub for /releases/latest, which only answers with a
   *stable* release. Every release of this repository is a pre-release, so the
   check always died with "HTTP 404". The updater now reads the release list
   and picks the newest entry that ships our asset.

2. features/sui_presets.lua enumerated its exports by hand and forgot
   buildListRows, so screens/sui_onboarding.lua:153 called nil on every fresh
   install. The export table must stay in sync with what siblings use.
"""
from pathlib import Path
import re
from lupa.lua54 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]
plugin = ROOT / "simpleui.koplugin"
updater_src = (plugin / "infra" / "sui_updater.lua").read_text()
presets_src = (plugin / "features" / "sui_presets.lua").read_text()

failures = []


def check(label, cond, detail=""):
    if cond:
        print("  ok   " + label)
    else:
        print("  FAIL " + label + ((" — " + detail) if detail else ""))
        failures.append(label)


# ---------------------------------------------------------------------------
# 1. Update discovery must not rely on the /releases/latest endpoint.
# ---------------------------------------------------------------------------
print("updater: release discovery")

api_url = re.search(r"local _API_URL = string\.format\(\s*\"([^\"]+)\"", updater_src)
check("_API_URL found", api_url is not None)
url_pattern = api_url.group(1) if api_url else ""
check("_API_URL asks for the release list", "releases?per_page=" in url_pattern, url_pattern)
check("_API_URL avoids /releases/latest", "/releases/latest" not in url_pattern, url_pattern)
check(
    "no /releases/latest link is offered to the user",
    re.search(r'"https://github\.com/%s/%s/releases/latest"', updater_src) is None,
    "the fallback link should open the releases page, not the missing stable release",
)

parse_src = re.search(r"local function _parseRelease\(body\).*?\nend\n", updater_src, re.S)
check("_parseRelease extracted", parse_src is not None)

lua = LuaRuntime(unpack_returned_tuples=True)
lua.globals().parse_source = parse_src.group(0) if parse_src else "return nil"
lua.execute(
    """
    ASSET_NAME = "simpleui.koplugin.zip"
    logger = {warn=function() end, info=function() end, err=function() end}
    payloads = {}
    json = {decode=function(body) return payloads[body] end}
    package.loaded["json"] = json
    """
)

# Build a fresh _parseRelease in its own environment so package.loaded tweaks
# (json present vs absent) take effect per call.
lua.execute(
    """
    function parse(body, use_json)
        package.loaded["json"] = use_json and json or nil
        local chunk = assert(load(parse_source .. "\\nreturn _parseRelease", "sui_updater_parse", "t", {
            ASSET_NAME=ASSET_NAME, logger=logger, require=require,
            ipairs=ipairs, type=type, tostring=tostring, pcall=pcall,
            string=string, table=table,
        }))
        return chunk()(body)
    end
    """
)

lua.execute(
    """
    local asset = {name="simpleui.koplugin.zip", browser_download_url="https://example.invalid/v3.zip"}
    local assetless = {name="source.tar.gz", browser_download_url="https://example.invalid/src.tar.gz"}

    -- Single-object payload (the shape /releases/latest used to return).
    payloads["single"] = {tag_name="v3.0.0-qiaomu.21", assets={asset}, html_url="h1"}

    -- List payload: newest first, and the newest one ships our asset.
    payloads["list"] = {
        {tag_name="v3.0.0-qiaomu.23", assets={asset}, html_url="h23"},
        {tag_name="v3.0.0-qiaomu.22", assets={asset}, html_url="h22"},
    }

    -- List payload where the newest release has no plugin ZIP yet.
    payloads["list_assetless_first"] = {
        {tag_name="v3.0.0-qiaomu.24", assets={assetless}, html_url="h24"},
        {tag_name="v3.0.0-qiaomu.23", assets={asset}, html_url="h23"},
    }

    -- Real GitHub list JSON (as text) for the regex fallback path.
    raw_list = '[{"tag_name":"v3.0.0-qiaomu.23","assets":[{"name":"simpleui.koplugin.zip",'
        .. '"browser_download_url":"https://example.invalid/v3.zip"}],"body":"notes"}]'
    """
)

cases = [
    ("single object payload", "single", True, "3.0.0-qiaomu.21", "https://example.invalid/v3.zip"),
    ("list payload picks newest with asset", "list", True, "3.0.0-qiaomu.23", "https://example.invalid/v3.zip"),
    (
        "list payload skips asset-less newest",
        "list_assetless_first",
        True,
        "3.0.0-qiaomu.23",
        "https://example.invalid/v3.zip",
    ),
]
def release_of(value):
    """lupa returns (nil, err) as a tuple when the parser bails out."""
    if isinstance(value, tuple):
        return value[0] if value else None
    return value


def field(tbl, name):
    if tbl is None:
        return None
    try:
        return tbl[name]
    except (TypeError, KeyError):
        return None


for label, key, use_json, want_ver, want_url in cases:
    got = release_of(lua.eval("parse")(key, use_json))
    version = field(got, "version")
    dl = field(got, "download_url")
    check(label, version == want_ver and dl == want_url, "got %s / %s" % (version, dl))

# Regex fallback (no json module) must still read an array payload.
raw = lua.eval("raw_list")
got = release_of(lua.eval("parse")(raw, False))
version = field(got, "version")
check(
    "regex fallback handles an array payload",
    version == "3.0.0-qiaomu.23",
    "got %s" % (version,),
)

# An empty release list must be reported, not crash.
lua.execute('payloads["empty"] = {}')
got = lua.eval("parse")("empty", True)
got_release = got[0] if isinstance(got, tuple) else got
got_err = got[1] if isinstance(got, tuple) and len(got) > 1 else None
check(
    "empty release list reports an error instead of crashing",
    got_release is None and bool(got_err),
    "got %r" % (got,),
)

# ---------------------------------------------------------------------------
# 2. Every module symbol used across modules must be exported.
# ---------------------------------------------------------------------------
print("presets: cross-module exports")

exports = re.search(r"\nreturn \{(.*)\n\}\n", presets_src, re.S)
check("export table extracted", exports is not None)
export_body = exports.group(1) if exports else ""

onboarding_src = (plugin / "screens" / "sui_onboarding.lua").read_text()
used = set(re.findall(r"SUIPresets\.([A-Za-z_][A-Za-z0-9_]*)", onboarding_src))
check("onboarding references SUIPresets symbols", bool(used), str(sorted(used)))

for name in sorted(used):
    exported = re.search(r"\b" + name + r"\s*=", export_body) is not None
    check("exports " + name, exported, "screens/sui_onboarding.lua calls it, export table lacks it")

lua.globals().plugin_root = str(plugin)
lua.execute("package.path = plugin_root .. '/?.lua;' .. package.path")
lua.execute(
    """
    package.loaded["infra/sui_store"] = {
        get=function(_,k,d) return d end, set=function() end,
        isTrue=function() return false end, readSetting=function(_,k,d) return d end,
        saveSetting=function() end,
    }
    package.loaded["logger"] = {warn=function() end, info=function() end, err=function() end}
    package.loaded["infra/sui_i18n"] = {translate=function(t) return t end}
    package.loaded["util"] = setmetatable({tableDeepCopy=function(t) return t end}, {__index=function() return function() end end})
    """
)
lua.execute('sui_presets = require("features/sui_presets")')
runtime_exports = lua.eval("sui_presets")
check(
    "buildListRows is callable through require()",
    lua.eval("type")(runtime_exports["buildListRows"]) == "function",
)

print()
if failures:
    print("FAILED (%d): %s" % (len(failures), ", ".join(failures)))
    raise SystemExit(1)
print("all updater/onboarding regression checks passed")
