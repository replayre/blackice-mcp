import sys

from blackice_mcp import paths


def test_client_install_is_not_guessed_off_windows():
    if sys.platform != "win32":
        assert paths.default_client_install() is None


def test_client_install_is_the_launcher_data_dir_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    monkeypatch.setattr(paths.platformdirs, "user_data_dir", lambda *a, **k: str(tmp_path))

    assert paths.default_client_install() == tmp_path


def test_config_and_cache_are_separate_directories():
    assert paths.config_dir() != paths.cache_dir()
    assert paths.default_profiles_path().parent == paths.config_dir()
