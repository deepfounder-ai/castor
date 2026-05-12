# Knowledge ingest — give the agent reading material

The **Knowledge** tab is where you teach the agent things it doesn't already know — product docs, internal wiki pages, PDFs of meeting notes, a YouTube transcript, a folder of contracts. Anything you ingest gets chunked, embedded, and stored in a vector database the agent can search through later.

50+ formats are supported via Microsoft **[MarkItDown](https://github.com/microsoft/markitdown)** with stdlib fallbacks. PDFs, Word docs, slides, spreadsheets, web pages — paste a URL or drag a file, qwe-qwe figures out the rest.

## Three ways to ingest

### 1. Drop files (Web UI)

Knowledge tab → drag files onto the upload zone, or click to pick. Files copy to `~/.qwe-qwe/uploads/kb/`, get a slug, and queue for chunking.

After ingest, each file shows up as a row with:

- **Filename + format** + size
- **Chunk count** (how many pieces it became — typically 1 per 800 chars)
- **Status** — pending / indexed / failed
- **Delete** button (removes the file + all chunks)

### 2. Paste URL

Knowledge tab → URL input → paste, hit **Index**. qwe-qwe fetches the page, runs it through MarkItDown for HTML → markdown conversion, and indexes the result.

Works for any `http://` / `https://` URL — articles, GitHub READMEs, docs sites, blog posts. Behind a login? Use the [visible browser](BROWSER.md) to scrape with your session, then `rag_index` the result.

YouTube URLs get special handling — `yt-dlp` extracts the transcript (native language preferred over auto-translated English), no need to type/paste a 30-minute talk.

### 3. Scan folder

Knowledge tab → **Scan folder** → pick a directory. qwe-qwe walks the folder, lists every indexable file with preview (size + detected format), and lets you batch-ingest in one click.

Use this for "import my Notion export" or "give the agent the whole `docs/` of this project."

## Supported formats

| Category | Formats |
|---|---|
| **Documents** | PDF · DOCX · PPTX · XLSX · EPUB · ODT · RTF · `.ipynb` |
| **Web** | HTML · any `https://` URL (fetch + markdown conversion) |
| **Data** | JSON · CSV · TSV · YAML · TOML · XML · INI · ENV |
| **Code** | Python, JS / TS, Go, Rust, Java / Kotlin / Scala, C / C++, Ruby, PHP, SQL, GraphQL, 40+ extensions |
| **Markup** | Markdown · reStructuredText · AsciiDoc · TeX |
| **Plain text** | `.txt`, `.log`, anything UTF-8 |
| **YouTube** | `youtube.com/watch?v=…` URLs — transcript via yt-dlp, with native-language preference |
| **Images** | PNG / JPG / WEBP — via vision model (OCR + description) |

If MarkItDown can't handle a file, qwe-qwe falls back to plain-text extraction. For binary formats it doesn't recognise, the file is staged but flagged "not indexed".

## How retrieval works

When the agent needs information, it can call `rag_search("query")` (activate via `tool_search("rag")`) — this hits the vector database and returns the top-K most relevant chunks. The chunks come with metadata:

- **Source filename / URL** — so the agent can cite where the info came from
- **Chunk index + total** — "section 3 of 12"
- **Distance score** — how relevant the chunk was (closer to 0 = better match)

The Web UI **Inspector** also shows recalled memories live during a turn — every `rag_search` populates the "Recalled memories" panel with the actual chunks the model saw. Useful for debugging "why did the agent say that?"

### Hybrid search

qwe-qwe runs three search modes in parallel and fuses the results:

1. **Dense vectors** (FastEmbed multilingual-MiniLM, 384d) — semantic similarity, works across paraphrases and 50+ languages
2. **Sparse vectors** (SPLADE++) — sparse-attention learned-sparse model; great at exact terminology
3. **BM25 FTS5** — SQLite full-text index; lexical matching, fastest

Results are fused via **Reciprocal Rank Fusion (RRF)** — a chunk that ranks well in two or three of the three layers wins over a chunk that only excels in one. Practical effect: less noise, fewer "this contains the word but isn't about what you asked" misses.

### Chunking

Files over 1000 characters get split into ~800-char chunks on sentence boundaries with ~100-char overlap. Each chunk is embedded separately and indexed.

Why chunking matters: the agent doesn't get the whole document, only the chunks that match. A 50-page PDF becomes 60 chunks; the search returns the 3 most-relevant chunks (default top-K). The model sees those 3, not all 60.

## Synthesis — the night job

Raw chunks are searchable immediately. But qwe-qwe also runs a **synthesis job** (default 03:00 daily, configurable) that:

1. Reads chunks tagged `synthesis_status=pending`
2. Asks the LLM to extract **entities** (product names, people, key concepts) and **relations** (X is part of Y, A uses B, etc.)
3. Writes **wiki pages** — synthesized markdown summaries that become higher-quality embeddings than the raw chunks they came from
4. Builds the **knowledge graph** — entities with typed relations, viewable in Web UI → Knowledge → Graph

After synthesis, `rag_search` returns **wiki chunks first** (best embeddings), then entity-relation expansions, then raw chunks. The agent gets "what" plus "context around it" in one query.

See [how-memory-works.md](how-memory-works.md) for the design-doc level detail.

## Tools the agent uses

Activate via `tool_search("rag")` or `tool_search("knowledge")`:

| Tool | What it does |
|---|---|
| `rag_search(query, top_k=5)` | Hybrid search across all indexed knowledge. |
| `rag_index(path_or_url)` | Index a file or URL. Same backend as the upload UI. |
| `rag_status()` | List ingested sources with chunk counts + synthesis status. |

The agent will use `rag_search` autonomously when your question implies it should look something up. Sometimes it overshoots and searches when memory alone would do — soul rule 8 (MEMORY DISCIPLINE) keeps this in check.

## Tags + filtering

Every chunk has a **tag** field. The default is `source:file` or `source:url`. Tags let you scope searches:

```python
rag_search("invoicing logic", tag="source:internal-docs")
```

[Presets](PRESET_GUIDE.md) use this — each preset has its own tag namespace, and activating a preset narrows `rag_search` to only that preset's knowledge.

## Privacy

- **All ingested content lives on your machine** under `~/.qwe-qwe/uploads/kb/` (raw files) and `~/.qwe-qwe/memory/` (Qdrant vectors).
- **Embedding is local** — FastEmbed (ONNX, CPU) runs on your CPU; no embedding API is called.
- **Synthesis uses your configured LLM** — if that's a cloud provider, the chunk content goes to that provider during the night synthesis job. Set `synthesis_enabled=0` in Settings to disable; raw search still works.
- **URL fetches are blocked from internal IPs** — `/api/knowledge/url` runs an SSRF check (`socket.getaddrinfo` + IP classification). Set `QWE_ALLOW_PRIVATE_URLS=1` to override for self-hosted Confluence / internal wikis on your LAN.
- **Telemetry is content-free** — only chunk counts and synthesis outcomes, never the chunk text.

## Configuration

**Settings → Memory** (web) or `EDITABLE_SETTINGS`:

| Setting | Default | What it does |
|---|---|---|
| `rag_chunk_size` | `800` | Target chunk length in chars |
| `rag_chunk_overlap` | `100` | Overlap between adjacent chunks |
| `rag_top_k` | `5` | How many chunks `rag_search` returns by default |
| `synthesis_enabled` | `1` | Toggle the nightly synthesis job |
| `synthesis_time` | `03:00` | When the synthesis job runs |
| `synthesis_provider` | (inherit) | Use a separate provider for synthesis (e.g. local model for heavy summarization) |
| `QWE_ALLOW_PRIVATE_URLS` | unset | Set to `1` to allow ingesting `http://10.0.0.5/wiki` etc. |

## Common patterns

### "Make this PDF available for the next conversations"

Knowledge tab → drag the PDF → done. Next time you ask about the content, the agent will `rag_search` and answer with citations.

### "Index my whole knowledge base"

Knowledge tab → Scan folder → point at the root → batch index. Schedule a nightly re-scan via [routines](ROUTINES.md) if the docs change often.

### "Find me everything I have on topic X"

Memory tab → search box. Returns chunks across all sources, ranked by relevance. Click a chunk to see its source file + neighbours.

### "I have a YouTube playlist of company-internal talks"

Paste each video URL into the URL input. yt-dlp pulls transcripts; native-language preferred (Russian transcripts won't be machine-translated to English).

## Troubleshooting

**Upload fails on a `.pdf`** — `pdfminer.six` or `pypdf` missing. `pip install pdfminer.six pypdf`. Doctor checks this.

**MarkItDown crashes on docx** — `python-docx` missing. `pip install python-docx`. Same pattern for `xlsx` (openpyxl), `pptx` (python-pptx).

**Search returns nothing relevant** — chunk count is 0 (`rag_status`)? The file failed silent. Check the file row for "failed" status. Try a different format (re-export to PDF, then try).

**YouTube fails with DRM error** — `yt-dlp` is shipped with multiple player clients (android / ios / web) that rotate to dodge blocks, but persistent failures mean YouTube blocked the IP. Wait or rotate IPs. Native-language transcript is preferred — if only auto-translated English is available, that's what you get.

**Folder scan never finishes** — large directories with binary blobs take a while; check `~/.qwe-qwe/logs/qwe-qwe.log` for per-file progress. The UI shows a running counter so you can watch the count climb.

**Synthesis didn't run** — check `synthesis_enabled=1` in Settings and that your provider is reachable at 03:00 local time. The synthesis job logs to `logs/qwe-qwe.log` with the marker `[synthesis]`.

## Cross-links

- [MEMORY.md](MEMORY.md) — how the agent's memory works alongside the knowledge base
- [how-memory-works.md](how-memory-works.md) — architecture deep-dive on the 3-layer system
- [PRESET_GUIDE.md](PRESET_GUIDE.md) — scoped knowledge per preset
- [PRIVACY.md](PRIVACY.md) — full data inventory + telemetry contract
