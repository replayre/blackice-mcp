import sys

import pytest

from blackice_mcp.supervisor import CLIENT, SERVER, load_profiles, parse_profile, save_profile


def test_missing_kind_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="kind"):
        parse_profile("local", {"install_dir": str(tmp_path)})


def test_client_kind_resolves_the_client_binary(tmp_path):
    profile = parse_profile("local", {"kind": CLIENT, "install_dir": str(tmp_path)})

    assert profile.binary().name.startswith("BlackICE")


def test_binary_name_follows_the_platform(tmp_path):
    server = parse_profile("local", {"kind": SERVER, "install_dir": str(tmp_path)})

    assert server.binary().name == ("REServer.exe" if sys.platform == "win32" else "REServer")


def test_unknown_kind_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="kind"):
        parse_profile("local", {"kind": "daemon", "install_dir": str(tmp_path)})


def test_missing_install_dir_is_rejected():
    with pytest.raises(ValueError, match="install_dir"):
        parse_profile("local", {"kind": SERVER})


def test_save_then_load_round_trips(tmp_path):
    config = tmp_path / "profiles.json"
    profile = parse_profile(
        "local",
        {
            "kind": SERVER,
            "install_dir": str(tmp_path),
            "bindaddr": "[::]:20770",
            "voip_addr": "[::]:20771",
        },
    )

    save_profile(config, profile)
    loaded = load_profiles(config)

    assert loaded["local"].install_dir == tmp_path
    assert loaded["local"].bindaddr == "[::]:20770"
    assert loaded["local"].voip_addr == "[::]:20771"


def test_saving_over_an_existing_profile_requires_overwrite(tmp_path):
    config = tmp_path / "profiles.json"
    profile = parse_profile("local", {"kind": SERVER, "install_dir": str(tmp_path)})

    save_profile(config, profile)

    with pytest.raises(ValueError, match="overwrite"):
        save_profile(config, profile)

    save_profile(config, profile, overwrite=True)
    assert "local" in load_profiles(config)


def test_saving_keeps_other_profiles(tmp_path):
    config = tmp_path / "profiles.json"
    save_profile(config, parse_profile("one", {"kind": SERVER, "install_dir": str(tmp_path)}))
    save_profile(config, parse_profile("two", {"kind": SERVER, "install_dir": str(tmp_path)}))

    assert sorted(load_profiles(config)) == ["one", "two"]


def test_load_of_a_missing_file_is_empty(tmp_path):
    assert load_profiles(tmp_path / "absent.json") == {}


def test_launcher_implies_a_windows_server_binary(tmp_path):
    native = parse_profile("a", {"kind": SERVER, "install_dir": str(tmp_path)})
    through_wine = parse_profile(
        "b", {"kind": SERVER, "install_dir": str(tmp_path), "launcher": ["wine"]}
    )

    assert native.binary().name in ("REServer", "REServer.exe")
    if sys.platform != "win32":
        assert native.binary().name == "REServer"
        assert through_wine.binary().name == "REServer.exe"


def test_command_prepends_the_launcher(tmp_path):
    profile = parse_profile(
        "local",
        {"kind": CLIENT, "install_dir": str(tmp_path), "launcher": ["wine"]},
    )

    command = profile.command(["--mcp"])

    assert command[0] == "wine"
    assert command[1].endswith("BlackICE.exe")
    assert command[2] == "--mcp"


def test_command_without_a_launcher_runs_the_binary_directly(tmp_path):
    profile = parse_profile("local", {"kind": SERVER, "install_dir": str(tmp_path)})

    command = profile.command(["--mcp"])

    assert command[0].endswith("REServer") or command[0].endswith("REServer.exe")
    assert command[1] == "--mcp"


def test_launcher_round_trips_through_save_and_load(tmp_path):
    config = tmp_path / "profiles.json"
    profile = parse_profile(
        "local",
        {"kind": CLIENT, "install_dir": str(tmp_path), "launcher": ["proton", "run"]},
    )

    save_profile(config, profile)

    assert load_profiles(config)["local"].launcher == ["proton", "run"]
