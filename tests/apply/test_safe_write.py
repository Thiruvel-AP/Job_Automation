"""safe_io.safe_write_text: create a NEW text file in the archive (the interview prep pack). Same rule as safe_copy: the operating system refuses an
existing destination, so nothing is ever overwritten."""
import hashlib

import pytest

from careerops.apply.safe_io import safe_write_text


def test_the_text_is_written_as_utf_8_and_the_digest_returned(tmp_path):
    dst = tmp_path / "prep.md"
    digest = safe_write_text(dst, "# Prep\nrésumé – ok\n")
    assert dst.read_bytes() == "# Prep\nrésumé – ok\n".encode("utf-8") and digest == hashlib.sha256(dst.read_bytes()).hexdigest()


def test_an_existing_file_folder_or_dangling_link_is_refused_and_left_alone(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("mine", encoding="utf-8")
    with pytest.raises(FileExistsError):
        safe_write_text(f, "new")
    assert f.read_text(encoding="utf-8") == "mine"
    (tmp_path / "d").mkdir()
    with pytest.raises(OSError):
        safe_write_text(tmp_path / "d", "new")
    link = tmp_path / "dangling.md"
    try:
        link.symlink_to(tmp_path / "nowhere")
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    with pytest.raises(FileExistsError):
        safe_write_text(link, "new")


def test_a_missing_parent_folder_is_not_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        safe_write_text(tmp_path / "missing" / "a.md", "x")
    assert not (tmp_path / "missing").exists()
