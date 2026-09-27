"""smart_grep syntax-aware chunking for JS/TS, Go, Rust and Java (tree-sitter)."""

from __future__ import annotations

import textwrap

import pytest

from code_puppy_core_plugins.jev_grep import treesitter
from code_puppy_core_plugins.jev_grep.chunks import source_chunks


def _ranges(text: str, path: str):
    chunks, parser = source_chunks(text, path)
    return [(c.line, c.end_line, c.symbol) for c in chunks], parser


def _assert_exact_coverage(text: str, path: str) -> None:
    chunks, _ = source_chunks(text, path)
    covered = {n for c in chunks for n in range(c.line, c.end_line + 1)}
    for number, line in enumerate(text.splitlines(), start=1):
        assert not line.strip() or number in covered, f"{path}:{number} uncovered"


GO = textwrap.dedent(
    """\
    package server

    import "net/http"

    type Server struct {
    \tmux *http.ServeMux
    }

    // Serve rejects expired sessions before routing.
    func (s *Server) Serve(w http.ResponseWriter, r *http.Request) {
    \tif expired(r) {
    \t\thttp.Error(w, "expired", http.StatusUnauthorized)
    \t\treturn
    \t}
    \ts.mux.ServeHTTP(w, r)
    }

    func expired(r *http.Request) bool {
    \treturn r.Header.Get("X-Expired") != ""
    }
    """
)

RUST = textwrap.dedent(
    """\
    use std::collections::HashMap;

    pub struct Cache {
        map: HashMap<String, u64>,
    }

    impl Cache {
        pub fn get(&self, key: &str) -> Option<&u64> {
            self.map.get(key)
        }

        pub fn evict_expired(&mut self, now: u64) {
            self.map.retain(|_, deadline| *deadline > now);
        }
    }
    """
)

JAVA = textwrap.dedent(
    """\
    package app;

    import java.util.Optional;

    @Service
    public class UserService {
        private final Repo repo;

        public UserService(Repo repo) {
            this.repo = repo;
        }

        public Optional<User> find(long id) {
            if (id < 0) {
                throw new IllegalArgumentException("negative id");
            }
            return repo.get(id);
        }
    }
    """
)

TS = textwrap.dedent(
    """\
    import { db } from "./db";

    /** Fetch one user or fail loudly. */
    export async function fetchUser(id: string): Promise<User> {
      if (!id) {
        throw new Error("missing id");
      }
      return db.users.get(id);
    }

    export const retry = async (fn: () => Promise<void>, attempts = 3) => {
      for (let i = 0; i < attempts; i++) {
        try {
          return await fn();
        } catch (err) {
          console.warn(err);
        }
      }
    };

    @Injectable()
    export class SessionService {
      constructor(private readonly store: Store) {}

      isExpired(session: Session): boolean {
        return session.expiresAt <= Date.now();
      }
    }
    """
)


JS = textwrap.dedent(
    """\
    const express = require("express");

    async function fetchUser(id) {
      if (!id) {
        throw new Error("missing id");
      }
      return db.users.get(id);
    }

    class Cache {
      get(key) {
        return this.map.get(key);
      }
    }

    module.exports = { fetchUser, Cache };
    """
)


def test_javascript_functions_and_classes():
    ranges, parser = _ranges(JS, "api.js")
    assert parser == "javascript"
    assert {"fetchUser", "Cache.get"} <= {symbol for _, _, symbol in ranges}


def test_go_methods_are_named_by_receiver():
    ranges, parser = _ranges(GO, "server.go")
    assert parser == "go"
    symbols = [symbol for _, _, symbol in ranges]
    assert "Server" in symbols  # type declaration named via its type_spec
    assert "Server.Serve" in symbols
    assert "expired" in symbols
    serve = next(r for r in ranges if r[2] == "Server.Serve")
    assert serve[0] <= 9 and serve[1] == 16  # leading comment absorbed


def test_rust_impl_blocks_split_into_methods():
    ranges, parser = _ranges(RUST, "cache.rs")
    assert parser == "rust"
    symbols = [symbol for _, _, symbol in ranges]
    assert {"Cache.get", "Cache.evict_expired"} <= set(symbols)
    assert "HashMap" not in symbols  # imports never earn a symbol boost


def test_java_class_members_and_annotations():
    ranges, parser = _ranges(JAVA, "UserService.java")
    assert parser == "java"
    by_symbol = {symbol: (start, end) for start, end, symbol in ranges}
    header = by_symbol["UserService"]
    assert header[0] <= 5 <= header[1]  # @Service annotation belongs to the class
    find = by_symbol["UserService.find"]
    assert find[0] <= 13 and find[1] >= 18  # whole method, plus absorbed gaps
    assert "UserService.UserService" in by_symbol  # constructor
    assert "Optional" not in by_symbol


def test_typescript_exports_arrows_and_classes():
    ranges, parser = _ranges(TS, "api.ts")
    assert parser == "typescript"
    symbols = [symbol for _, _, symbol in ranges]
    assert {"fetchUser", "retry", "SessionService.isExpired"} <= set(symbols)
    assert "SessionService.constructor" in symbols


@pytest.mark.parametrize(
    "text, path, parser",
    [
        (TS, "api.ts", "typescript"),
        (TS.replace("@Injectable()\n", ""), "api.tsx", "tsx"),
        (JS, "api.js", "javascript"),
        (GO, "server.go", "go"),
        (RUST, "cache.rs", "rust"),
        (JAVA, "UserService.java", "java"),
    ],
)
def test_every_language_covers_every_line(text, path, parser):
    assert source_chunks(text, path)[1] == parser
    _assert_exact_coverage(text, path)


def test_long_function_splits_into_blocks_with_full_coverage():
    body = "\n".join(
        f"  if (x === {i}) {{\n    a = {i};\n    b = {i};\n    c = {i};\n    d = {i};\n  }}"
        for i in range(6)
    )
    text = f"export function route(x) {{\n{body}\n}}\n"
    ranges, _ = _ranges(text, "router.js")
    assert len(ranges) > 1
    assert {symbol for _, _, symbol in ranges} == {"route"}
    _assert_exact_coverage(text, "router.js")


@pytest.mark.parametrize("text, path", [(TS, "api.ts"), (GO, "server.go")])
def test_windows_line_endings_give_identical_snippets(text, path):
    assert _ranges(text, path) == _ranges(text.replace("\n", "\r\n"), path)


def test_unparseable_file_falls_back_to_windows():
    ranges, parser = _ranges("export function oops( {\n  return 1;\n", "broken.ts")
    assert parser == "overlapping-lines"
    assert ranges == [(1, 2, None)]


def test_unsupported_extension_uses_windows():
    assert source_chunks("# Title\n\nprose\n", "notes.md")[1] == "overlapping-lines"


def test_known_bad_tree_sitter_is_refused(monkeypatch):
    """0.26.x segfaults natively; it must degrade to windows, never be used."""
    treesitter._parser.cache_clear()
    monkeypatch.setattr(treesitter, "version", lambda name: "0.26.0")
    try:
        assert treesitter._tree_sitter_is_safe() is False
        assert source_chunks(GO, "server.go")[1] == "overlapping-lines"
    finally:
        treesitter._parser.cache_clear()


def test_missing_grammar_falls_back(monkeypatch):
    def broken_loader():
        raise ImportError("grammar wheel not available on this platform")

    treesitter._parser.cache_clear()
    monkeypatch.setitem(treesitter.LANGUAGES, ".go", ("go", broken_loader))
    try:
        assert source_chunks(GO, "server.go")[1] == "overlapping-lines"
    finally:
        treesitter._parser.cache_clear()
