"""Paths in reports are relative to the project, however the file was found."""

import os

from django.test import override_settings

from signal_guard.receivers import relative_path


def test_path_under_the_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert relative_path(str(tmp_path / "app" / "x.py")) == os.path.join("app", "x.py")


def test_path_outside_the_project_stays_absolute(tmp_path, monkeypatch):
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path / "project")
    other = str(tmp_path / "elsewhere" / "x.py")
    assert relative_path(other) == other


def test_path_reached_through_a_symlink(tmp_path, monkeypatch):
    # The project is opened through a symlink, but Python imported the file
    # by its real path (or the other way round).
    real = tmp_path / "real"
    (real / "app").mkdir(parents=True)
    (real / "app" / "conftest.py").write_text("")
    link = tmp_path / "link"
    link.symlink_to(real)

    monkeypatch.chdir(link)
    monkeypatch.setenv("PWD", str(link))
    expected = os.path.join("app", "conftest.py")
    assert relative_path(str(real / "app" / "conftest.py")) == expected
    monkeypatch.chdir(real)
    assert relative_path(str(link / "app" / "conftest.py")) == expected


def test_path_under_base_dir_when_run_from_elsewhere(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "app").mkdir(parents=True)
    (project / "app" / "x.py").write_text("")
    (tmp_path / "outside").mkdir()
    monkeypatch.chdir(tmp_path / "outside")
    with override_settings(BASE_DIR=project):
        assert relative_path(str(project / "app" / "x.py")) == os.path.join(
            "app", "x.py"
        )
