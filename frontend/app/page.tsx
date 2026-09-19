"use client";

import { useEffect, useRef, useState } from "react";
import {
  ask,
  Citation,
  getHealth,
  getStats,
  Health,
  indexAll,
  search,
  SearchHit,
  Stats,
  uploadFiles,
} from "./api";

type Mode = "ask" | "search";

export default function Home() {
  const [mode, setMode] = useState<Mode>("ask");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);

  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [results, setResults] = useState<SearchHit[]>([]);
  const [streaming, setStreaming] = useState(false);

  const [stats, setStats] = useState<Stats | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [notice, setNotice] = useState<string>("");
  const fileInput = useRef<HTMLInputElement>(null);

  async function refreshStatus() {
    try {
      setStats(await getStats());
      setHealth(await getHealth());
    } catch {
      /* backend may not be up yet */
    }
  }

  useEffect(() => {
    refreshStatus();
  }, []);

  async function run() {
    if (!query.trim() || busy) return;
    setBusy(true);
    setAnswer("");
    setCitations([]);
    setResults([]);

    try {
      if (mode === "ask") {
        setStreaming(true);
        await ask(
          query,
          (c) => setCitations(c),
          (t) => setAnswer((prev) => prev + t)
        );
        setStreaming(false);
      } else {
        const res = await search(query);
        setResults(res.results);
      }
    } catch (e: any) {
      setNotice(`Error: ${e.message ?? e}`);
    } finally {
      setBusy(false);
      setStreaming(false);
    }
  }

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setNotice("Uploading…");
    try {
      const up = await uploadFiles(files);
      setNotice(`Uploaded ${up.saved.length} file(s). Indexing…`);
      const res = await indexAll();
      setNotice(`Indexed. ${res.total_chunks} total chunks in archive.`);
      refreshStatus();
    } catch (e: any) {
      setNotice(`Upload/index failed: ${e.message ?? e}`);
    }
  }

  const modelsReady =
    health?.models.llm_ready && health?.models.embed_ready;

  return (
    <div className="container">
      <div className="header">
        <div>
          <div className="title">
            Archive <span>AI Search</span>
          </div>
          <div className="subtitle">
            Hybrid semantic + keyword search over your local archive, curated by
            Bonsai 27B
          </div>
        </div>
        <div className="stats">
          {stats && (
            <span className="stat">
              <b>{stats.total_chunks}</b> chunks ·{" "}
              <b>{Object.keys(stats.sources).length}</b> sources
            </span>
          )}
        </div>
      </div>

      {!modelsReady && health && (
        <div className="panel" style={{ borderColor: "var(--danger)" }}>
          <div className="muted">
            Models not fully ready. LLM ready:{" "}
            {String(health.models.llm_ready)} · Embeddings ready:{" "}
            {String(health.models.embed_ready)}. Make sure Ollama is running and
            both models are pulled.
          </div>
        </div>
      )}

      <div className="modes">
        <div
          className={`chip ${mode === "ask" ? "active" : ""}`}
          onClick={() => setMode("ask")}
        >
          Ask (AI answer)
        </div>
        <div
          className={`chip ${mode === "search" ? "active" : ""}`}
          onClick={() => setMode("search")}
        >
          Search (raw chunks)
        </div>
      </div>

      <div className="searchbar">
        <input
          className="input"
          placeholder={
            mode === "ask"
              ? "Ask anything about your archive…"
              : "Search for terms, names, topics…"
          }
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
        />
        <button className="btn" onClick={run} disabled={busy}>
          {busy ? <span className="spinner" /> : mode === "ask" ? "Ask" : "Search"}
        </button>
      </div>

      {notice && <div className="muted" style={{ marginBottom: 16 }}>{notice}</div>}

      {/* AI answer */}
      {mode === "ask" && (answer || streaming) && (
        <div className="panel">
          <div className="section-label">Answer</div>
          <div className="answer">
            {answer}
            {streaming && <span className="cursor" />}
          </div>
        </div>
      )}

      {/* Citations */}
      {mode === "ask" && citations.length > 0 && (
        <div className="panel">
          <div className="section-label">Sources</div>
          {citations.map((c) => (
            <div className="citation" key={c.index}>
              <span className="cite-index">[{c.index}]</span>
              <span>
                <b>{c.source_file}</b>
                {c.location ? ` — ${c.location}` : ""}{" "}
                <span className="muted">
                  {c.matched_by.join(" + ")}
                </span>
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Raw search results */}
      {mode === "search" && results.length > 0 && (
        <div className="panel">
          <div className="section-label">{results.length} results</div>
          {results.map((r) => (
            <div className="result" key={r.id}>
              <div className="result-meta">
                <b style={{ color: "var(--text)" }}>
                  {r.metadata.source_file}
                </b>
                <span>{r.metadata.location}</span>
                <span className="tag">{r.metadata.file_type}</span>
                {r.matched_by.map((m) => (
                  <span key={m} className={`tag ${m}`}>
                    {m}
                  </span>
                ))}
              </div>
              <div className="result-text">
                {r.text.length > 400 ? r.text.slice(0, 400) + "…" : r.text}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Upload / index */}
      <div className="panel">
        <div className="section-label">Add to archive</div>
        <div
          className="dropzone"
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            onUpload(e.dataTransfer.files);
          }}
        >
          Drop files here or click to upload
          <div className="muted" style={{ marginTop: 6 }}>
            {stats
              ? `Supported: ${stats.supported_extensions.join(", ")}`
              : "PDF, docx, csv, epub, txt, md, html"}
          </div>
        </div>
        <input
          ref={fileInput}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={(e) => onUpload(e.target.files)}
        />
      </div>
    </div>
  );
}
