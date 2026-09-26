import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Ask_View Style_Selector + Bibliography_Block tests (task 13.4).
// Validates: Req 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.10, 6.11.
//
// Mocks the API client the same way page.test.tsx does, additionally mocking
// `ask` (to drive citations) and `formatCitations` (to feed the bibliography).

vi.mock("../api", () => {
  return {
    getStats: vi.fn(),
    getHealth: vi.fn(),
    getSelectableSources: vi.fn(),
    listGroups: vi.fn(),
    search: vi.fn(),
    ask: vi.fn(),
    formatCitations: vi.fn(),
    createGroup: vi.fn(),
    renameGroup: vi.fn(),
    deleteGroup: vi.fn(),
    addSources: vi.fn(),
    removeSources: vi.fn(),
    uploadFiles: vi.fn(),
    importFolder: vi.fn(),
    indexAll: vi.fn(),
  };
});

import Home from "../page";
import * as api from "../api";
import type { Citation, FormattedCitation } from "../api";

const SOURCES = [
  { source_path: "/archive/alpha.pdf", chunks: 12 },
  { source_path: "/archive/beta.pdf", chunks: 7 },
];

// Two cited chunks, two distinct source_paths (plus a duplicate to prove
// dedupe on the distinct-source mapping).
const CITATIONS: Citation[] = [
  {
    index: 1,
    source_file: "alpha.pdf",
    source_path: "/archive/alpha.pdf",
    location: "p.1",
    file_type: "pdf",
    matched_by: ["semantic"],
    rrf_score: 0.9,
  },
  {
    index: 2,
    source_file: "beta.pdf",
    source_path: "/archive/beta.pdf",
    location: "p.2",
    file_type: "pdf",
    matched_by: ["keyword"],
    rrf_score: 0.8,
  },
  {
    index: 3,
    source_file: "alpha.pdf",
    source_path: "/archive/alpha.pdf",
    location: "p.5",
    file_type: "pdf",
    matched_by: ["semantic"],
    rrf_score: 0.7,
  },
];

const FORMATTED: FormattedCitation[] = [
  {
    source_path: "/archive/alpha.pdf",
    text: "Alpha, A. Alpha Book. 2020.",
    leading_element: "Alpha",
    incomplete: false,
    missing_required: [],
  },
  {
    source_path: "/archive/beta.pdf",
    text: "Beta, B. Beta Article. 2021.",
    leading_element: "Beta",
    incomplete: false,
    missing_required: [],
  },
];

function mockDefaults() {
  vi.mocked(api.getStats).mockResolvedValue({
    total_chunks: 19,
    sources: { "/archive/alpha.pdf": 12, "/archive/beta.pdf": 7 },
    supported_extensions: [".pdf"],
    indexed_files: 2,
    manifest: {},
  } as any);
  vi.mocked(api.getHealth).mockResolvedValue({
    status: "ok",
    models: {
      ollama_reachable: true,
      llm_model: "llm",
      llm_ready: true,
      embed_model: "embed",
      embed_ready: true,
      installed_models: [],
    },
  } as any);
  vi.mocked(api.getSelectableSources).mockResolvedValue({ sources: SOURCES } as any);
  vi.mocked(api.listGroups).mockResolvedValue({ groups: [] } as any);
  vi.mocked(api.search).mockResolvedValue({ results: [] } as any);
  vi.mocked(api.formatCitations).mockResolvedValue({
    style: "MLA",
    citations: FORMATTED,
  } as any);
  // ask() emits the citation set (and a tiny summary token) then resolves.
  vi.mocked(api.ask).mockImplementation(async (_q: string, handlers: any) => {
    handlers.onCitations(CITATIONS);
    handlers.onSection("summary");
    handlers.onToken("summary", "An answer.");
  });
}

async function renderPage() {
  render(<Home />);
  await waitFor(() => expect(api.getSelectableSources).toHaveBeenCalled());
  await waitFor(() => expect(api.listGroups).toHaveBeenCalled());
}

// Runs an ask so citations/bibliography state is populated.
async function runAsk(user: ReturnType<typeof userEvent.setup>) {
  const input = screen.getByPlaceholderText(/Ask anything/i);
  await user.clear(input);
  await user.type(input, "what is alpha?");
  await user.click(screen.getByRole("button", { name: "Ask" }));
  await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));
  // Sources panel confirms citations landed in state.
  await waitFor(() => expect(screen.getByText("Sources")).toBeInTheDocument());
}

function styleSelect() {
  return screen.getByLabelText("Citation style") as HTMLSelectElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockDefaults();
});

describe("Style_Selector (Req 6.1, 6.2)", () => {
  it("renders exactly Off/MLA/APA and defaults to Off", async () => {
    await renderPage();
    const sel = styleSelect();
    const options = within(sel).getAllByRole("option") as HTMLOptionElement[];
    expect(options.map((o) => o.textContent)).toEqual(["Off", "MLA", "APA"]);
    expect(options.map((o) => o.value)).toEqual(["off", "MLA", "APA"]);
    // Default selection is Off.
    expect(sel.value).toBe("off");
  });
});

describe("Bibliography_Block visibility (Req 6.3, 6.4, 6.5)", () => {
  it("renders no block while the selector is Off, even with cited sources", async () => {
    const user = userEvent.setup();
    await renderPage();
    await runAsk(user);

    expect(styleSelect().value).toBe("off");
    expect(screen.queryByText("Works Cited")).not.toBeInTheDocument();
    expect(screen.queryByText("References")).not.toBeInTheDocument();
    expect(api.formatCitations).not.toHaveBeenCalled();
  });

  it("renders no block when a style is chosen but there are zero cited sources", async () => {
    const user = userEvent.setup();
    // ask() emits an empty citation list this time.
    vi.mocked(api.ask).mockImplementation(async (_q: string, handlers: any) => {
      handlers.onCitations([]);
      handlers.onSection("summary");
      handlers.onToken("summary", "No sources.");
    });
    await renderPage();

    const input = screen.getByPlaceholderText(/Ask anything/i);
    await user.type(input, "empty?");
    await user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(api.ask).toHaveBeenCalledTimes(1));

    await user.selectOptions(styleSelect(), "MLA");

    // No cited sources -> no block, and no format call.
    expect(screen.queryByText("Works Cited")).not.toBeInTheDocument();
    expect(api.formatCitations).not.toHaveBeenCalled();
  });
});

describe("Bibliography_Block titles and content (Req 6.4, 6.6, 6.7)", () => {
  it("shows 'Works Cited' with formatted entries when MLA is selected", async () => {
    const user = userEvent.setup();
    await renderPage();
    await runAsk(user);

    await user.selectOptions(styleSelect(), "MLA");

    await waitFor(() => expect(screen.getByText("Works Cited")).toBeInTheDocument());
    // Formatted with the distinct source paths (deduped: alpha + beta).
    expect(api.formatCitations).toHaveBeenLastCalledWith(
      ["/archive/alpha.pdf", "/archive/beta.pdf"],
      "MLA"
    );
    expect(screen.getByText("Alpha, A. Alpha Book. 2020.")).toBeInTheDocument();
    expect(screen.getByText("Beta, B. Beta Article. 2021.")).toBeInTheDocument();
  });

  it("shows 'References' when APA is selected", async () => {
    const user = userEvent.setup();
    vi.mocked(api.formatCitations).mockResolvedValue({
      style: "APA",
      citations: FORMATTED,
    } as any);
    await renderPage();
    await runAsk(user);

    await user.selectOptions(styleSelect(), "APA");

    await waitFor(() => expect(screen.getByText("References")).toBeInTheDocument());
    expect(api.formatCitations).toHaveBeenLastCalledWith(
      ["/archive/alpha.pdf", "/archive/beta.pdf"],
      "APA"
    );
  });
});

describe("Re-render on style change without resubmitting (Req 6.10)", () => {
  it("switching the style re-runs formatCitations, not ask()/the ask endpoint", async () => {
    const user = userEvent.setup();
    await renderPage();
    await runAsk(user);
    expect(api.ask).toHaveBeenCalledTimes(1);

    // Off -> MLA
    await user.selectOptions(styleSelect(), "MLA");
    await waitFor(() => expect(api.formatCitations).toHaveBeenCalledTimes(1));
    expect(api.formatCitations).toHaveBeenLastCalledWith(
      ["/archive/alpha.pdf", "/archive/beta.pdf"],
      "MLA"
    );

    // MLA -> APA re-formats again...
    await user.selectOptions(styleSelect(), "APA");
    await waitFor(() => expect(api.formatCitations).toHaveBeenCalledTimes(2));
    expect(api.formatCitations).toHaveBeenLastCalledWith(
      ["/archive/alpha.pdf", "/archive/beta.pdf"],
      "APA"
    );

    // ...and never re-submits the question.
    expect(api.ask).toHaveBeenCalledTimes(1);
  });
});

describe("Selector independence (Req 6.11)", () => {
  it("inline [n] markers / Sources list render regardless of the selector", async () => {
    const user = userEvent.setup();
    await renderPage();
    await runAsk(user);

    // Sources list present with inline [1] and [2] markers while Off.
    expect(screen.getByText("Sources")).toBeInTheDocument();
    expect(screen.getByText("[1]")).toBeInTheDocument();
    expect(screen.getByText("[2]")).toBeInTheDocument();

    // Turning the selector on does not disturb the Sources list.
    await user.selectOptions(styleSelect(), "MLA");
    await waitFor(() => expect(screen.getByText("Works Cited")).toBeInTheDocument());
    expect(screen.getByText("Sources")).toBeInTheDocument();
    expect(screen.getByText("[1]")).toBeInTheDocument();
    expect(screen.getByText("[2]")).toBeInTheDocument();

    // Back to Off hides the block but keeps the Sources list.
    await user.selectOptions(styleSelect(), "off");
    await waitFor(() => expect(screen.queryByText("Works Cited")).not.toBeInTheDocument());
    expect(screen.getByText("Sources")).toBeInTheDocument();
    expect(screen.getByText("[1]")).toBeInTheDocument();
  });
});
