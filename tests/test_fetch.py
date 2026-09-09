from dob.config import DEFAULT_URL_HOSTS
from dob.fetch import canonical_ref, classify_url

EXT = {".pdf", ".mp3", ".md"}


def test_canonical_refs():
    assert canonical_ref("https://youtu.be/dQw4w9WgXcQ") == "youtube:dQw4w9WgXcQ"
    assert (
        canonical_ref("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10") == "youtube:dQw4w9WgXcQ"
    )
    assert canonical_ref("https://youtube.com/shorts/dQw4w9WgXcQ") == "youtube:dQw4w9WgXcQ"
    assert (
        canonical_ref("https://www.loom.com/share/0123456789abcdef0123456789abcdef?x=1")
        == "loom:0123456789abcdef0123456789abcdef"
    )
    assert (
        canonical_ref("https://drive.google.com/file/d/1AbC_dEf/view?usp=sharing")
        == "gdrive:1AbC_dEf"
    )
    assert canonical_ref("https://docs.google.com/document/d/1AbC/edit") == "gdoc:document:1AbC"
    assert canonical_ref("https://example.com/a.pdf#page=2") == "https://example.com/a.pdf"


def test_classify():
    hosts = list(DEFAULT_URL_HOSTS)
    assert classify_url("https://youtu.be/dQw4w9WgXcQ", hosts, EXT) == "ytdlp"
    assert classify_url("https://m.youtube.com/watch?v=x", hosts, EXT) == "ytdlp"
    assert classify_url("https://docs.google.com/spreadsheets/d/1/edit", hosts, EXT) == "gdoc"
    assert classify_url("https://drive.google.com/file/d/1/view", hosts, EXT) == "ytdlp"
    assert classify_url("https://example.com/report.PDF", hosts, EXT) == "http"
    assert classify_url("https://example.com/", hosts, EXT) is None
    assert classify_url("https://evil.com/x.exe", hosts, EXT) is None
    assert classify_url("ftp://youtube.com/x", hosts, EXT) is None
