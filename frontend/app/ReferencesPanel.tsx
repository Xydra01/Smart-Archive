"use client";

// References_View (citation-formatting tasks 12.1–12.4).
//
// A collapsible panel — consistent with the Groups manager in page.tsx — that
// lists every indexed source, lets the user edit its bibliographic metadata,
// previews the formatted citation live, and imports references.
//
// Design decisions documented inline where the spec allows a choice:
//   - authors are edited as a NEWLINE-separated list (one author per line).
//   - the live preview formats the LAST-SAVED record (formatCitations formats
//     the stored record), so edits appear in the preview after a successful
//     save. This keeps the preview a single source of truth with the backend
//     formatter and avoids coupling preview to an implicit save. The preview
//     also re-fetches on style change and refreshes within ~1s.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CitationStyle,
  FormattedCitation,
  ImportFormat,
  listReferences,
  formatCitations,
  importReference,
  SourceMetadata,
  SourceType,
  updateReference,
  UpdatableReferenceFields,
} from "./api";
import { isIsoCalendarDate } from "./dateValidation";

const SOURCE_TYPES: SourceType[] = ["book", "article", "website", "report", "other"];
const MAX_FIELD_LEN = 2000;
const SAVE_TIMEOUT_MS = 10_000;
const PREVIEW_DEBOUNCE_MS = 500; // within ~1s of an edit (Req 5.10)

// The editable text fields (authors is handled separately as a list).
type TextFieldKey =
  | "title"
  | "container"
  | "publisher"
  | "publication_date"
  | "url"
  | "access_date";

const TEXT_FIELDS: { key: TextFieldKey; label: string }[] = [
  { key: "title", label: "Title" },
  { key: "container", label: "Container (journal / site)" },
  { key: "publisher", label: "Publisher" },
  { key: "publication_date", label: "Publication date (YYYY-MM-DD)" },
  { key: "url", label: "URL" },
  { key: "access_date", label: "Access date (YYYY-MM-DD)" },
];

const IMPORT_FORMATS: { value: ImportFormat; label: string }[] = [
  { value: "bibtex", label: "BibTeX" },
  { value: "ris", label: "RIS" },
  { value: "csljson", label: "CSL-JSON" },
  { value: "verbatim", label: "Verbatim" },
];

// The per-row draft the edit form binds to. All strings so inputs stay
// controlled; authors is the newline-joined list.
interface Draft {
  source_type: SourceType;
  authors: string;
  title: string;
  container: string;
  publisher: string;
  publication_date: string;
  url: string;
  access_date: string;
}

function draftFromRecord(rec: SourceMetadata): Draft {
  return {
    source_type: rec.source_type,
    authors: rec.authors.join("\n"),
    title: rec.title ?? "",
    container: rec.container ?? "",
    publisher: rec.publisher ?? "",
    publication_date: rec.publication_date ?? "",
    url: rec.url ?? "",
    access_date: rec.access_date ?? "",
  };
}

// A permissive URL check: accept an http(s) URL that the URL constructor can
// parse. Empty is allowed (the field is optional); non-empty must parse.
function isValidUrl(value: string): boolean {
  const v = value.trim();
  if (v === "") return true;
  try {
    const u = new URL(v);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

// A date field is valid when empty (optional) or a valid ISO calendar date.
function isValidDateField(value: string): boolean {
  const v = value.trim();
  return v === "" || isIsoCalendarDate(v);
}

// Splits the newline/semicolon-separated authors textarea into a trimmed,
// non-empty list. (Newlines are primary; semicolons also split so a pasted
// "A; B" works.)
function parseAuthors(raw: string): string[] {
  return raw
    .split(/[\n;]/)
    .map((a) => a.trim())
    .filter((a) => a.length > 0);
}

// Empty marker shown for an absent field (Req 5.3).
const EMPTY = "—";

// Builds the update payload from a draft (task 12.2). Blank strings become
// null so the backend clears the field; authors becomes the parsed list.
function fieldsFromDraft(draft: Draft): UpdatableReferenceFields {
  const blankToNull = (s: string): string | null => {
    const t = s.trim();
    return t === "" ? null : t;
  };
  return {
    source_type: draft.source_type,
    authors: parseAuthors(draft.authors),
    title: blankToNull(draft.title),
    container: blankToNull(draft.container),
    publisher: blankToNull(draft.publisher),
    publication_date: blankToNull(draft.publication_date),
    url: blankToNull(draft.url),
    access_date: blankToNull(draft.access_date),
  };
}

// Races a promise against a timeout so a hung save surfaces an error and keeps
// the user's entered values (Req 5.7). AbortController is signalled on timeout
// as well, so if the underlying fetch honored a signal it would cancel; since
// api.ts's updateReference does not accept a signal, Promise.race is what
// actually enforces the deadline here.
function withTimeout<T>(
  work: (signal: AbortSignal) => Promise<T>,
  ms: number
): Promise<T> {
  const controller = new AbortController();
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      controller.abort();
      reject(new Error(`Timed out after ${ms / 1000}s`));
    }, ms);
    work(controller.signal).then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (e) => {
        clearTimeout(timer);
        reject(e);
      }
    );
  });
}

interface RowProps {
  record: SourceMetadata;
  previewStyle: CitationStyle;
  onSaved: (rec: SourceMetadata) => void;
}

function ReferenceRow({ record, previewStyle, onSaved }: RowProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>(() => draftFromRecord(record));
  const [errors, setErrors] = useState<Partial<Record<TextFieldKey, string>>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string>("");

  // Live preview state (task 12.3).
  const [preview, setPreview] = useState<FormattedCitation | null>(null);
  const [previewError, setPreviewError] = useState<string>("");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Import control state (task 12.4).
  const [showImport, setShowImport] = useState(false);
  const [importFormat, setImportFormat] = useState<ImportFormat>("bibtex");
  const [importPayload, setImportPayload] = useState("");
  const [importEntry, setImportEntry] = useState("");
  const [importStyle, setImportStyle] = useState<CitationStyle>("MLA");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState("");

  // When the record changes (e.g. after a save or import), re-seed the draft
  // only if we are not mid-edit so we don't clobber unsaved input.
  useEffect(() => {
    if (!editing) setDraft(draftFromRecord(record));
  }, [record, editing]);

  // Debounced live preview: formats the LAST-SAVED record in the selected
  // style. Refreshes when the row enters edit mode, when the style changes, and
  // when the stored record changes (after a save). Edits to the draft appear in
  // the preview after a successful save (documented tradeoff above).
  useEffect(() => {
    if (!editing) {
      setPreview(null);
      setPreviewError("");
      return;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      try {
        const res = await formatCitations([record.source_path], previewStyle);
        setPreview(res.citations[0] ?? null);
        setPreviewError("");
      } catch (e: any) {
        setPreviewError(e?.message ?? "preview failed");
      }
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [editing, previewStyle, record]);

  function setField(key: keyof Draft, value: string) {
    setDraft((d) => ({ ...d, [key]: value }));
  }

  // Validate url + date fields before sending (Req 5.8). Returns the invalid
  // field map; empty means valid.
  function validate(d: Draft): Partial<Record<TextFieldKey, string>> {
    const errs: Partial<Record<TextFieldKey, string>> = {};
    if (!isValidUrl(d.url)) errs.url = "Enter a valid http(s) URL.";
    if (!isValidDateField(d.publication_date))
      errs.publication_date = "Use a valid YYYY-MM-DD date.";
    if (!isValidDateField(d.access_date))
      errs.access_date = "Use a valid YYYY-MM-DD date.";
    return errs;
  }

  async function onSave() {
    setSaveError("");
    const errs = validate(draft);
    setErrors(errs);
    // If any field is invalid, flag it and DO NOT send the save; entered values
    // are preserved because the draft state is untouched (Req 5.8).
    if (Object.keys(errs).length > 0) return;

    setSaving(true);
    try {
      const saved = await withTimeout(
        () => updateReference(record.source_path, fieldsFromDraft(draft)),
        SAVE_TIMEOUT_MS
      );
      // Reflect the saved values in the row (Req 5.6).
      onSaved(saved);
      setDraft(draftFromRecord(saved));
      setSaveError("");
    } catch (e: any) {
      // Failure or timeout: show the error, keep entered values (Req 5.7).
      setSaveError(e?.message ?? "save failed");
    } finally {
      setSaving(false);
    }
  }

  async function onImport() {
    setImportError("");
    if (!importPayload.trim()) {
      setImportError("Paste the reference payload first.");
      return;
    }
    setImporting(true);
    try {
      const body: {
        format: ImportFormat;
        payload: string;
        entry?: string;
        style?: CitationStyle;
      } = { format: importFormat, payload: importPayload };
      if (importFormat !== "verbatim" && importEntry.trim())
        body.entry = importEntry.trim();
      if (importFormat === "verbatim") body.style = importStyle;
      const updated = await importReference(record.source_path, body);
      // Reflect the returned view (title/display_name/verbatim_overrides).
      onSaved(updated);
      setDraft(draftFromRecord(updated));
      setImportPayload("");
      setImportEntry("");
      setImportError("");
    } catch (e: any) {
      // Show the backend's error message on 400 (parse failure / multi-entry).
      setImportError(e?.message ?? "import failed");
    } finally {
      setImporting(false);
    }
  }

  const missing = record.missing_required;

  return (
    <div className="ref-row">
      <div className="ref-row-head">
        <span className="ref-name" title={record.source_path}>
          {record.display_name}
        </span>
        <span className="ref-path muted">{record.source_path}</span>
        <button
          className="link-btn"
          onClick={() => {
            setEditing((e) => !e);
            setSaveError("");
            setErrors({});
          }}
        >
          {editing ? "Close" : "Edit"}
        </button>
      </div>

      {/* Effective metadata summary with explicit empty markers (Req 5.3). */}
      <div className="ref-summary muted">
        <span>Type: {record.source_type}</span>
        <span>Authors: {record.authors.length ? record.authors.join("; ") : EMPTY}</span>
        <span>Title: {record.title ?? EMPTY}</span>
        <span>Container: {record.container ?? EMPTY}</span>
        <span>Publisher: {record.publisher ?? EMPTY}</span>
        <span>Published: {record.publication_date ?? EMPTY}</span>
        <span>URL: {record.url ?? EMPTY}</span>
        <span>Accessed: {record.access_date ?? EMPTY}</span>
      </div>

      {/* Name each field still required for the current source_type (Req 5.9). */}
      {missing.length > 0 && (
        <div className="ref-missing">
          Missing required for “{record.source_type}”: {missing.join(", ")}
        </div>
      )}

      {editing && (
        <div className="ref-edit">
          <label className="ref-field">
            <span>Source type</span>
            <select
              className="select"
              value={draft.source_type}
              onChange={(e) => setField("source_type", e.target.value as SourceType)}
            >
              {SOURCE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>

          <label className="ref-field">
            <span>Authors (one per line)</span>
            <textarea
              className="input ref-textarea"
              rows={3}
              maxLength={MAX_FIELD_LEN}
              value={draft.authors}
              onChange={(e) => setField("authors", e.target.value)}
            />
          </label>

          {TEXT_FIELDS.map((f) => (
            <label className="ref-field" key={f.key}>
              <span>{f.label}</span>
              <input
                className="input"
                maxLength={MAX_FIELD_LEN}
                value={draft[f.key]}
                onChange={(e) => setField(f.key, e.target.value)}
                aria-invalid={errors[f.key] ? true : undefined}
              />
              {errors[f.key] && <span className="ref-error">{errors[f.key]}</span>}
            </label>
          ))}

          <div className="row">
            <button className="btn secondary" onClick={onSave} disabled={saving}>
              {saving ? <span className="spinner" /> : "Save"}
            </button>
            {saveError && <span className="ref-error">{saveError}</span>}
          </div>

          {/* Live citation preview (task 12.3). */}
          <div className="ref-preview">
            <div className="section-label">Preview ({previewStyle})</div>
            {previewError ? (
              <span className="ref-error">{previewError}</span>
            ) : preview ? (
              <div className="ref-preview-text">
                {preview.text}
                {preview.incomplete && (
                  <span className="muted"> (incomplete)</span>
                )}
              </div>
            ) : (
              <span className="muted">Loading preview…</span>
            )}
            <div className="muted ref-preview-note">
              Preview reflects the last saved values; edits appear after Save.
            </div>
          </div>

          {/* Import control (task 12.4). */}
          <div className="ref-import">
            <button className="link-btn" onClick={() => setShowImport((s) => !s)}>
              {showImport ? "Hide import" : "Import reference"}
            </button>
            {showImport && (
              <div className="ref-import-body">
                <label className="ref-field">
                  <span>Format</span>
                  <select
                    className="select"
                    value={importFormat}
                    onChange={(e) => setImportFormat(e.target.value as ImportFormat)}
                  >
                    {IMPORT_FORMATS.map((f) => (
                      <option key={f.value} value={f.value}>
                        {f.label}
                      </option>
                    ))}
                  </select>
                </label>

                {importFormat !== "verbatim" && (
                  <label className="ref-field">
                    <span>Entry (index or key, for multi-entry files)</span>
                    <input
                      className="input"
                      value={importEntry}
                      onChange={(e) => setImportEntry(e.target.value)}
                    />
                  </label>
                )}

                {importFormat === "verbatim" && (
                  <label className="ref-field">
                    <span>Style</span>
                    <select
                      className="select"
                      value={importStyle}
                      onChange={(e) => setImportStyle(e.target.value as CitationStyle)}
                    >
                      <option value="MLA">MLA</option>
                      <option value="APA">APA</option>
                    </select>
                  </label>
                )}

                <label className="ref-field">
                  <span>Payload</span>
                  <textarea
                    className="input ref-textarea"
                    rows={5}
                    value={importPayload}
                    onChange={(e) => setImportPayload(e.target.value)}
                    placeholder="Paste BibTeX / RIS / CSL-JSON / verbatim text…"
                  />
                </label>

                <div className="row">
                  <button
                    className="btn secondary"
                    onClick={onImport}
                    disabled={importing}
                  >
                    {importing ? <span className="spinner" /> : "Import"}
                  </button>
                  {importError && <span className="ref-error">{importError}</span>}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function ReferencesPanel() {
  const [references, setReferences] = useState<SourceMetadata[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  // The panel's own preview style toggle; defaults to MLA (Req 1.4).
  const [previewStyle, setPreviewStyle] = useState<CitationStyle>("MLA");

  const load = useCallback(async () => {
    try {
      const res = await listReferences();
      setReferences(res.references);
      setLoadError("");
    } catch (e: any) {
      setLoadError(e?.message ?? "failed to load references");
    } finally {
      setLoaded(true);
    }
  }, []);

  // Load on mount (the panel is only mounted when opened, so this fires on open).
  useEffect(() => {
    load();
  }, [load]);

  const onSaved = useCallback((rec: SourceMetadata) => {
    setReferences((prev) =>
      prev.map((r) => (r.source_path === rec.source_path ? rec : r))
    );
  }, []);

  const styleToggle = useMemo(
    () => (
      <label className="row" style={{ gap: 6 }}>
        <span className="muted">Preview style</span>
        <select
          className="select"
          value={previewStyle}
          onChange={(e) => setPreviewStyle(e.target.value as CitationStyle)}
        >
          <option value="MLA">MLA</option>
          <option value="APA">APA</option>
        </select>
      </label>
    ),
    [previewStyle]
  );

  return (
    <div className="references-view">
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        {styleToggle}
      </div>

      {loadError && <div className="ref-error">{loadError}</div>}

      {!loaded ? (
        <div className="muted">Loading references…</div>
      ) : references.length === 0 ? (
        // No-sources empty state (Req 5.2).
        <div className="muted">No indexed sources yet.</div>
      ) : (
        <div className="ref-list">
          {references.map((r) => (
            <ReferenceRow
              key={r.source_path}
              record={r}
              previewStyle={previewStyle}
              onSaved={onSaved}
            />
          ))}
        </div>
      )}
    </div>
  );
}
