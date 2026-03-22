from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path

import pytest

from pring.export.pyg_export import export_placeholder
from pring.io.ftp_cache import FtpCache
from pring.io.iri import IRIBuilder
from pring.io.rdf_stream import stream_ntriples
from pring.utils.logging_utils import setup_logging


def test_iribuilder_creates_stable_uri_and_does_not_override_existing_uri():
    iri = IRIBuilder(base="https://kg.example/")
    assert iri.node_uri("Protein", "P 123/45") == "https://kg.example/Protein/P12345"
    props = iri.attach_uri("Protein", "P12345", {"uri": "keep-me", "x": 1})
    assert props == {"uri": "keep-me", "x": 1}


def test_stream_ntriples_reads_plain_and_gzipped_files(tmp_path: Path):
    text = "<s> <p> <o> .\nnot a triple\n"
    plain = tmp_path / "x.nt"
    plain.write_text(text, encoding="utf-8")
    gz = tmp_path / "x.nt.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as f:
        f.write(text)

    assert [(t.s, t.p, t.o) for t in stream_ntriples(plain)] == [("<s>", "<p>", "<o>")]
    assert [(t.s, t.p, t.o) for t in stream_ntriples(gz)] == [("<s>", "<p>", "<o>")]


def test_ftp_cache_put_get_and_purge(tmp_path: Path):
    cache = FtpCache(tmp_path / "cache", ttl_seconds=1)
    src = tmp_path / "data.txt"
    src.write_text("demo", encoding="utf-8")
    dst = cache.put_file(src, "ftp://example.org/demo", suffix=".txt")
    assert dst.exists()
    assert cache.get("ftp://example.org/demo", suffix=".txt") == dst

    old = time.time() - 10
    Path(dst).touch()
    import os
    os.utime(dst, (old, old))
    assert cache.get("ftp://example.org/demo", suffix=".txt") is None
    assert cache.purge() == 1


def test_setup_logging_creates_rotating_log_file_and_replaces_handlers(tmp_path: Path):
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler())
    log_path = setup_logging(log_dir=tmp_path / "logs", console_level="WARNING", file_level="INFO")
    logging.getLogger("pring.test").info("hello")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert log_path.exists()
    assert "hello" in log_path.read_text(encoding="utf-8")


def test_export_placeholder_writes_readme(tmp_path: Path):
    export_placeholder(tmp_path / "pyg")
    assert "PyG export stub" in (tmp_path / "pyg" / "README.txt").read_text(encoding="utf-8")
