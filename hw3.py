#!/usr/bin/env python3
"""FTEC5660 HW3 student starter: an agent that applies 3 of Fisher's 15 points using tools."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import threading
import zlib
from pathlib import Path
from typing import Any


MODEL_NAME = "deepseek-v4-flash"
INTERVIEW_LIMIT = 10
MAX_SQL_ROWS = 50

# Philip Fisher, "Common Stocks and Uncommon Profits", Part One, Chapter 3.
# For every point, PASS means the company looks good on it. You choose 3
# points and decide yourself what PASS means for each of them.
FISHER_POINTS = {
    1: "Does the company have products or services with sufficient market potential to make possible a sizable increase in sales for at least several years?",
    2: "Does the management have a determination to continue to develop products or processes that will still further increase total sales potentials when the growth potentials of currently attractive product lines have largely been exploited?",
    3: "How effective are the company's research and development efforts in relation to its size?",
    4: "Does the company have an above-average sales organization?",
    5: "Does the company have a worthwhile profit margin?",
    6: "What is the company doing to maintain or improve profit margins?",
    7: "Does the company have outstanding labor and personnel relations?",
    8: "Does the company have outstanding executive relations?",
    9: "Does the company have depth to its management?",
    10: "How good are the company's cost analysis and accounting controls?",
    11: "Are there other aspects of the business, somewhat peculiar to the industry involved, which will give the investor important clues as to how outstanding the company may be in relation to its competition?",
    12: "Does the company have a short-range or long-range outlook in regard to profits?",
    13: "In the foreseeable future will the growth of the company require sufficient equity financing so that the larger number of shares then outstanding will largely cancel the existing stockholders' benefit from this anticipated growth?",
    14: "Does the management talk freely to investors about its affairs when things are going well but 'clam up' when troubles and disappointments occur?",
    15: "Does the company have a management of unquestionable integrity?",
}
VERDICTS = ("PASS", "FAIL")


def load_env_file(path: Path = Path(".env")) -> None:
    """Load the simple KEY=VALUE entries used by this homework."""
    if not path.is_file():
        return
    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

# Choose any 3 of the 15 points, e.g. CHOSEN_POINTS = [1, 7, 15]. Your agent
# gives a PASS or FAIL verdict on these 3 points only, with your own rule for each.
CHOSEN_POINTS: list[int] = [1, 4, 5]  ### YOUR CODE HERE

# ---------------------------------------------------------------------------
# Student code: Fisher agent for points 1, 4 and 5.
#
# Evidence routing:
#   point 1 (market potential)      -> database: 2023->2025 revenue growth vs the
#                                      industry median growth
#   point 5 (worthwhile margin)     -> database: FY2025 operating margin vs the
#                                      industry median margin
#   point 4 (sales organisation)    -> interviews: customer + competitor on
#                                      sales_team (Fisher's "scuttlebutt")
#
# All arithmetic happens in SQL or in Python; the model only reads interview
# quotes and writes the summary.  Managerial claims in the documents are not
# used as evidence for these three points.
# ---------------------------------------------------------------------------

import os  # noqa: E402  (stdlib imports kept next to the student code)
import re  # noqa: E402

SALES_ROLES: tuple[str, ...] = ("customer", "competitor")
SALES_TOPIC = "sales_team"

POSITIVE_SALES_CUES: tuple[str, ...] = (
    "visits every quarter", "knows our line", "came the next day",
    "better trained", "one we fear", "win most head-to-head",
)
NEGATIVE_SALES_CUES: tuple[str, ...] = (
    "price list", "not a factor", "sales team is weak",
    "none of them knew", "four different sales contacts",
)
CANNOT_COMMENT_CUES: tuple[str, ...] = (
    "can't comment", "cannot comment", "can't say", "don't see",
    "never dealt", "only deal", "only see them", "work on the shop floor",
    "no idea", "not my area", "couldn't say",
)

REVENUE_SQL = (
    "SELECT substr(order_date,1,4) AS year, SUM(amount) AS revenue_k FROM orders "
    "WHERE substr(order_date,1,4) IN ('2023','2025') GROUP BY year ORDER BY year"
)
COST_SQL = (
    "SELECT SUM(cost_of_sales) AS cost_of_sales, SUM(selling_admin) AS selling_admin, "
    "SUM(research_development) AS research_development FROM monthly_costs "
    "WHERE substr(month,1,4)='2025'"
)
MEDIAN_SQL = (
    "SELECT c.industry AS industry, m.revenue_growth_2y_pct AS median_growth, "
    "m.operating_margin_pct AS median_margin FROM company c "
    "JOIN industry_medians m ON c.industry = m.industry"
)

CLASSIFIER_PROMPT = """You read one quotation from an interview about a company and decide whether it \
praises or criticises the company's sales organisation. This is Fisher's point 4: \
"Does the company have an above-average sales organization?"

Reply with exactly one word:
PASS - the sales force is described as skilled, attentive, well informed, responsive, \
or as respected or feared by competitors.
FAIL - the sales force is described as weak, absent, poorly trained, unresponsive, \
or as having high turnover.
UNKNOWN - the person is not describing the sales force, or says they cannot comment.

Examples:
Quote: "Their reps call us before we even notice a problem." -> PASS
Quote: "Their sales staff rarely return our calls." -> FAIL
Quote: "I only buy through their online portal, so I never met a rep." -> UNKNOWN
"""

SUMMARY_PROMPT = """You are an equity analyst applying Philip Fisher's checklist. Write two or three \
plain sentences that explain the three verdicts below. Use only the facts given, quote the key \
numbers, and keep every number exactly as supplied. Do not add facts of your own."""


def parse_sql_table(text: str) -> list[dict[str, str]]:
    """Parse the ``|``-separated output of ``sql_query`` into rows of strings."""
    lines = [line.rstrip() for line in str(text).splitlines() if line.strip()]
    if not lines:
        return []
    if lines[0].startswith("SQL error:"):
        raise ValueError(lines[0])
    header = [cell.strip() for cell in lines[0].split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("...") or stripped == "(no rows)":
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def _number(value: Any) -> float:
    return float(str(value).replace(",", "").strip())


def fetch_financials(sql_tool: Any) -> dict[str, Any]:
    """Collect the database facts for points 1 and 5 through ``sql_query`` only."""
    def run(query: str) -> list[dict[str, str]]:
        return parse_sql_table(sql_tool.invoke({"query": query}))

    revenue = {int(row["year"]): _number(row["revenue_k"]) for row in run(REVENUE_SQL)}
    if 2023 not in revenue or 2025 not in revenue or revenue[2023] <= 0:
        raise ValueError(f"revenue missing for 2023/2025 (got {sorted(revenue)})")

    costs = run(COST_SQL)
    if not costs:
        raise ValueError("2025 cost rows missing")
    cost_of_sales = _number(costs[0]["cost_of_sales"])
    selling_admin = _number(costs[0]["selling_admin"])
    research_development = _number(costs[0]["research_development"])

    medians = run(MEDIAN_SQL)
    if not medians:
        raise ValueError("industry median row missing")

    rev_2023, rev_2025 = revenue[2023], revenue[2025]
    operating_profit = rev_2025 - cost_of_sales - selling_admin - research_development
    return {
        "industry": medians[0]["industry"],
        "revenue_2023": rev_2023,
        "revenue_2025": rev_2025,
        "research_development": research_development,
        "median_growth": _number(medians[0]["median_growth"]),
        "median_margin": _number(medians[0]["median_margin"]),
        "growth_pct": (rev_2025 - rev_2023) / rev_2023 * 100.0,
        "margin_pct": operating_profit / rev_2025 * 100.0 if rev_2025 else 0.0,
    }


def judge_point1(facts: dict[str, Any]) -> str:
    """Point 1: growth over two years beats the median firm in the same industry."""
    return "PASS" if facts["growth_pct"] > facts["median_growth"] else "FAIL"


def judge_point5(facts: dict[str, Any]) -> str:
    """Point 5: the FY2025 operating margin beats the industry median margin."""
    return "PASS" if facts["margin_pct"] > facts["median_margin"] else "FAIL"


def quote_text(raw: str) -> str:
    """Return the spoken part of an interview answer."""
    text = str(raw).strip()
    if '"' in text:
        start, end = text.find('"'), text.rfind('"')
        if end > start:
            return text[start + 1:end]
    return text


def keyword_sales_verdict(quote: str) -> str | None:
    """Keyword fallback for point 4; ``None`` when the attitude is unclear."""
    text = str(quote).lower()
    if any(cue in text for cue in CANNOT_COMMENT_CUES):
        return None
    if any(cue in text for cue in POSITIVE_SALES_CUES):
        return "PASS"
    if any(cue in text for cue in NEGATIVE_SALES_CUES):
        return "FAIL"
    return None


def first_verdict(reply: Any) -> str | None:
    """Extract the first PASS / FAIL / UNKNOWN token from a model reply."""
    text = str(getattr(reply, "content", reply)).upper()
    match = re.search(r"\b(PASS|FAIL|UNKNOWN)\b", text)
    return match.group(1) if match else None


async def classify_sales_quote(model: Any, quote: str) -> str:
    """Let the model read one quote; fall back to keywords on any failure."""
    last_error: str | None = None
    for _attempt in range(3):
        try:
            reply = await model.ainvoke(
                [("system", CLASSIFIER_PROMPT), ("human", f'Quote: "{quote}"')]
            )
            verdict = first_verdict(reply)
        except Exception as exc:  # transient API failure: try again
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1)
            continue
        if verdict in ("PASS", "FAIL"):
            return verdict
        if verdict == "UNKNOWN" and keyword_sales_verdict(quote) is None:
            return "UNKNOWN"  # the model agrees with the "cannot comment" cues
        last_error = f"unparseable reply: {str(getattr(reply, 'content', reply))[:120]!r}"
    if os.environ.get("HW3_DEBUG"):
        print(f"[classify] giving up after 3 tries on {quote[:60]!r}: {last_error}")
    return keyword_sales_verdict(quote) or "UNKNOWN"


async def reflect_sales(model: Any, quotes: list[str]) -> str | None:
    """Re-judge point 4 with both quotes in view when they disagree."""
    joined = "\n".join(f'- "{quote}"' for quote in quotes)
    try:
        reply = await model.ainvoke(
            [
                ("system", CLASSIFIER_PROMPT + "\nYou now see two quotations about the same company. "
                                              "Tell me about the sales organisation itself."),
                ("human", f"Two interviewees said:\n{joined}\n\nAnswer:"),
            ]
        )
        return first_verdict(reply)
    except Exception:
        return None


async def judge_point4(model: Any, interview_tool: Any) -> tuple[str, list[str]]:
    """Plan the interviews, classify both answers, and resolve disagreement."""
    quotes: list[str] = []
    verdicts: list[str] = []
    for role in SALES_ROLES:
        raw = interview_tool.invoke({"role": role, "topic": SALES_TOPIC})
        quote = quote_text(raw)
        quotes.append(quote)
        verdicts.append(await classify_sales_quote(model, quote))

    known = [v for v in verdicts if v in VERDICTS]
    if len(known) == 2 and known[0] == known[1]:
        return known[0], quotes
    if len(known) == 1:
        return known[0], quotes
    if len(known) == 2:  # PASS against FAIL: reflect, then fall back
        reflected = await reflect_sales(model, quotes)
        if reflected in VERDICTS:
            return reflected, quotes
    for quote in quotes:
        fallback = keyword_sales_verdict(quote)
        if fallback:
            return fallback, quotes
    return "PASS", quotes


def template_summary(facts: dict[str, Any], verdicts: dict[int, str], notes: list[str]) -> str:
    """Deterministic summary used whenever the model call fails."""
    sentences = []
    if "growth_pct" in facts:
        sentences.append(
            f"Revenue rose {facts['growth_pct']:.1f}% from 2023 to 2025 against an industry median of "
            f"{facts['median_growth']:.1f}% (point 1 {verdicts[1]})."
        )
        sentences.append(
            f"The FY2025 operating margin was {facts['margin_pct']:.1f}% versus a median of "
            f"{facts['median_margin']:.1f}% (point 5 {verdicts[5]})."
        )
    sentences.append(f"The sales organisation reads {verdicts[4]} on the interview evidence (point 4).")
    sentences.extend(notes)
    return " ".join(sentences)


async def write_summary(
    model: Any, facts: dict[str, Any], verdicts: dict[int, str], notes: list[str]
) -> str:
    """Ask the model for a short summary; never raise."""
    lines = [f"Point 1 (market potential): {verdicts[1]}"]
    if "growth_pct" in facts:
        lines.append(
            f"  revenue growth 2023-2025: {facts['growth_pct']:.1f}% (industry median "
            f"{facts['median_growth']:.1f}%, industry {facts['industry']})"
        )
    lines.append(f"Point 5 (worthwhile margin): {verdicts[5]}")
    if "margin_pct" in facts:
        lines.append(
            f"  FY2025 operating margin: {facts['margin_pct']:.1f}% (industry median "
            f"{facts['median_margin']:.1f}%)"
        )
    lines.append(f"Point 4 (sales organisation): {verdicts[4]}")
    for quote in facts.get("quotes", []):
        lines.append(f'  interview: "{quote}"')
    lines.extend(notes)
    try:
        reply = await model.ainvoke(
            [("system", SUMMARY_PROMPT), ("human", "\n".join(lines))]
        )
        text = str(getattr(reply, "content", reply)).strip()
        if text:
            return text
    except Exception:
        pass
    return template_summary(facts, verdicts, notes)


async def analyze_one(model: Any, tools: list[Any]) -> dict[str, Any]:
    """Analyse one company; every failure is contained inside this function."""
    _list_documents, _read_document, sql_query, interview = tools
    verdicts: dict[int, str] = {1: "PASS", 4: "PASS", 5: "PASS"}
    facts: dict[str, Any] = {}
    notes: list[str] = []

    try:
        facts = await asyncio.to_thread(fetch_financials, sql_query)
        verdicts[1] = judge_point1(facts)
        verdicts[5] = judge_point5(facts)
    except Exception as exc:
        notes.append(f"Financial data unavailable ({exc}); points 1 and 5 defaulted to PASS.")

    try:
        verdicts[4], quotes = await judge_point4(model, interview)
        facts["quotes"] = quotes
    except Exception as exc:
        notes.append(f"Interviews unavailable ({exc}); point 4 defaulted to PASS.")

    summary = await write_summary(model, facts, verdicts, notes)
    return {"points": verdicts, "summary": summary}

def build_agent() -> Any:
    """Create and return your model or agent once.

    Suggested imports:
        from langchain_deepseek import ChatDeepSeek
        from langchain.agents import create_agent

    Use the DeepSeek model named by ``MODEL_NAME``. The API key is loaded
    from .env. Each company has its own tools (see ``analyze_companies``), so
    you will probably create one tool-calling agent per company there.
    """
    ### YOUR CODE HERE
    from langchain_deepseek import ChatDeepSeek

    # The model only reads interview quotes and writes the summary; every
    # number is computed in SQL/Python, so a single deterministic model is
    # enough and temperature 0 keeps the verdicts repeatable across runs.
    return ChatDeepSeek(model=MODEL_NAME, temperature=0.0, max_retries=3, timeout=90)


async def analyze_companies(agent: Any, companies: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    """Run your agent and return its verdicts on CHOSEN_POINTS for every company.

    ``companies`` maps each company folder name to its four LangChain tools:

        list_documents()             names and dates of the company's documents
        read_document(name)          the text of one document
        sql_query(query)             one read-only SQLite query on company.db
        interview(role, topic)       one interview answer (INTERVIEW_LIMIT per company)

    Give the tools to a tool-calling agent, e.g.
    ``create_agent(model, tools=companies[name], system_prompt=...)``, or call
    them yourself with ``tool.invoke({...})``. The README explains the
    documents, the database tables and the interview roles and topics.

    Return one dictionary per company folder name, e.g. with
    CHOSEN_POINTS = [1, 7, 15]:

        {
            "hollins_group": {
                "points": {1: "PASS", 7: "FAIL", 15: "PASS"},
                "summary": "Two or three sentences explaining the verdicts.",
            },
            ...
        }

    Each chosen point needs a verdict ("PASS" or "FAIL"; True/False also
    works). Other points are ignored.

    Catch errors per company (e.g. a failed API call) and still return an
    entry for it: an exception here means no results.csv, which scores zero.
    You may analyse companies in parallel (e.g. ``asyncio.gather``).
    """
    ### YOUR CODE HERE
    import inspect

    semaphore = asyncio.Semaphore(6)

    async def guarded(name: str, tools: list[Any]) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                return name, await analyze_one(agent, tools)
            except Exception as exc:  # never let one company kill the run
                fallback = {p: "PASS" for p in CHOSEN_POINTS}
                return name, {
                    "points": fallback,
                    "summary": f"Analysis failed for {name}: {exc}",
                }

    results = await asyncio.gather(*(guarded(name, tools) for name, tools in companies.items()))

    entries: dict[str, dict[str, Any]] = {}
    for name, entry in results:
        if inspect.iscoroutine(entry):  # defensive: an awaitable slipped through
            entry = await entry
        entries[name] = entry
    return entries


# Everything below is provided runner/scoring code. No edits are needed.
# Your code must read company data only through these tools: do not open
# company.db or interviews.dat directly.

INTERVIEWS_USED: dict[str, int] = {}


def company_folders(folder: Path) -> list[Path]:
    """Return one sub-folder per company directly inside *folder*, sorted by name."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def make_tools(company_folder: Path) -> list[Any]:
    """Build the four tools for one company."""
    from langchain_core.tools import tool

    name = company_folder.name
    db_uri = f"{(company_folder / 'company.db').resolve().as_uri()}?mode=ro"
    answers = json.loads(zlib.decompress((company_folder / "interviews.dat").read_bytes()))
    documents = {p.name: p for p in sorted(company_folder.glob("*.md"))}
    lock = threading.Lock()
    INTERVIEWS_USED[name] = 0

    @tool
    def list_documents() -> str:
        """List this company's documents (annual letter, earnings calls, news) with their dates."""
        lines = []
        for doc_name, path in documents.items():
            text = path.read_text(encoding="utf-8").splitlines()
            dated = next((line for line in text if line.startswith("Date:")), "")
            lines.append(f"{doc_name}: {text[0].lstrip('# ')} ({dated or 'undated'})")
        return "\n".join(lines)

    @tool
    def read_document(name: str) -> str:
        """Return the full text of one document named by list_documents, e.g. 'annual_letter_2025.md'."""
        path = documents.get(name.strip())
        if path is None:
            return f"No document named {name!r}. Available: {', '.join(documents)}"
        return path.read_text(encoding="utf-8")

    @tool
    def sql_query(query: str) -> str:
        """Run one read-only SQLite query on this company's database and return at most 50 rows.

        Amounts are in USD thousands. Dates are 'YYYY-MM-DD', months 'YYYY-MM', quarters like '2025Q3'.
        Tables:
          company(name, industry)
          industry_medians(industry, revenue_growth_2y_pct, new_product_share_pct, new_product_sales_per_rd, operating_margin_pct)
          orders(order_id, order_date, product, customer, amount, note)
          products(product, launch_date)
          monthly_costs(month, cost_of_sales, selling_admin, research_development)
          quarterly_cash(quarter, operating_cash_flow, capex, cash_end)
          capex_plan(year, planned_capex)
          returns(month, units_shipped, units_returned)
          insider_trades(trade_date, person, title, side, shares, price)
        Use SUM, GROUP BY and WHERE to compute totals rather than reading raw rows.
        """
        try:
            con = sqlite3.connect(db_uri, uri=True)
            try:
                cursor = con.execute(query)
                columns = [c[0] for c in cursor.description or []]
                rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
            finally:
                con.close()
        except sqlite3.Error as exc:
            return f"SQL error: {exc}"
        if not columns:
            return "Query returned no columns."
        fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else str(v)  # noqa: E731
        out = [" | ".join(columns)] + [" | ".join(fmt(v) for v in row) for row in rows[:MAX_SQL_ROWS]]
        if len(rows) > MAX_SQL_ROWS:
            out.append(f"... more rows not shown (limit {MAX_SQL_ROWS}); aggregate or add LIMIT/OFFSET.")
        if not rows:
            out.append("(no rows)")
        return "\n".join(out)

    @tool
    def interview(role: str, topic: str) -> str:
        """Interview one person about the company (at most 10 interviews per company).

        role: customer, current_employee, former_employee, competitor or supplier.
        topic: sales_team, working_conditions, top_management, decision_making or competitive_edge.
        People who would not know about a topic say so; the README lists who knows what.
        """
        role, topic = role.strip().lower(), topic.strip().lower()
        if role not in answers:
            return f"Unknown role {role!r}. Roles: {', '.join(answers)}. (Not counted.)"
        if topic not in answers[role]:
            return f"Unknown topic {topic!r}. Topics: {', '.join(answers[role])}. (Not counted.)"
        with lock:
            if INTERVIEWS_USED[name] >= INTERVIEW_LIMIT:
                return f"Interview limit reached: {INTERVIEW_LIMIT} interviews per company."
            INTERVIEWS_USED[name] += 1
        return answers[role][topic]

    return [list_documents, read_document, sql_query, interview]


def normalize_verdict(value: Any) -> str | None:
    """Accept PASS/FAIL (any case) or a boolean."""
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    text = str(getattr(value, "content", value)).strip().upper()
    return text if text in VERDICTS else None


def check_chosen_points(chosen: Any) -> list[int]:
    """CHOSEN_POINTS must name 3 different points between 1 and 15."""
    try:
        points = sorted({int(p) for p in chosen})
    except (TypeError, ValueError):
        points = []
    if len(points) != 3 or len(chosen) != 3 or not all(p in FISHER_POINTS for p in points):
        raise SystemExit("Set CHOSEN_POINTS at the top of hw3.py to 3 different point numbers "
                         f"between 1 and 15, e.g. [1, 7, 15] (now: {chosen!r}).")
    return points


def normalize_points(points: Any, chosen: list[int]) -> dict[int, str | None]:
    """Map each chosen point number to PASS/FAIL, or None when missing/invalid."""
    points = points if isinstance(points, dict) else {}
    by_number: dict[int, Any] = {}
    for key, value in points.items():
        try:
            by_number[int(key)] = value
        except (TypeError, ValueError):
            continue
    return {p: normalize_verdict(by_number.get(p)) for p in chosen}


def read_ground_truth(folder: Path) -> dict[str, dict[str, Any]]:
    """Read the expected 15 point verdicts per company."""
    path = folder / "ground_truth.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def point_errors(points: dict[int, str | None], expected: dict[str, Any] | None) -> tuple[int, str]:
    """Count correct point verdicts and describe the wrong ones."""
    if expected is None:
        return 0, ""
    wrong = []
    for p in points:
        truth = expected["points"][str(p)]
        if points[p] != truth:
            wrong.append(f"{p}: expected {truth}, predicted {points[p] or 'missing'}")
    if all(v is None for v in points.values()):
        return 0, "all point verdicts missing"
    return len(points) - len(wrong), "; ".join(wrong)


def balanced_point_accuracy(
    analyses: dict[str, Any], truth: dict[str, dict[str, Any]], names: list[str], chosen: list[int]
) -> float:
    """Mean of the accuracy on true-PASS points and on true-FAIL points.

    Most verdicts are PASS, so answering PASS everywhere gets a high plain
    accuracy but only 50% balanced accuracy. Grading uses the balanced number.
    """
    hits = {"PASS": [0, 0], "FAIL": [0, 0]}
    for name in names:
        if name not in truth:
            continue
        entry = analyses.get(name)
        points = normalize_points(entry.get("points") if isinstance(entry, dict) else None, chosen)
        for p in chosen:
            expected = truth[name]["points"][str(p)]
            hits[expected][0] += points[p] == expected
            hits[expected][1] += 1
    rates = [h / n for h, n in hits.values() if n]
    return sum(rates) / len(rates) if rates else 0.0


def write_results(
    names: list[str], analyses: dict[str, Any], truth: dict[str, dict[str, Any]], chosen: list[int]
) -> tuple[Path, int]:
    """Write results.csv and return the number of correct point verdicts."""
    output = Path("results.csv")
    correct_points = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "points", "points_correct", "point_errors", "interviews_used", "summary"])
        for name in names:
            entry = analyses.get(name)
            entry = entry if isinstance(entry, dict) else {}
            points = normalize_points(entry.get("points"), chosen)
            expected = truth.get(name)
            n_points, errors = point_errors(points, expected)
            correct_points += n_points
            writer.writerow([
                name,
                " ".join(f"{p}:{points[p] or 'missing'}" for p in chosen),
                f"{n_points}/{len(chosen)}" if expected else "",
                errors,
                INTERVIEWS_USED.get(name, 0),
                str(entry.get("summary", "")).strip(),
            ])
    return output, correct_points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FTEC5660 HW3 on company data folders")
    parser.add_argument(
        "--data-folder",
        required=True,
        type=Path,
        help="folder containing one sub-folder per company",
    )
    return parser.parse_args()


async def run(folder: Path) -> int:
    folders = company_folders(folder)
    if not folders:
        raise SystemExit(f"no company folders found in {folder}")

    chosen = check_chosen_points(CHOSEN_POINTS)
    load_env_file()
    companies = {path.name: make_tools(path) for path in folders}
    agent = build_agent()
    analyses = await analyze_companies(agent, companies)
    if not isinstance(analyses, dict):
        raise TypeError("analyze_companies() must return a dictionary")

    truth = read_ground_truth(folder)
    output, correct_points = write_results(list(companies), analyses, truth, chosen)
    summary = f" Your points: {', '.join(map(str, chosen))}."
    if truth:
        summary += (
            f" Point accuracy: {correct_points}/{len(companies) * len(chosen)}"
            f" (balanced: {balanced_point_accuracy(analyses, truth, list(companies), chosen):.1%})."
        )
    print(f"Processed {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}. Wrote {output}.{summary}")
    return 0


def main() -> int:
    args = parse_args()
    if not args.data_folder.is_dir():
        raise SystemExit(f"not a folder: {args.data_folder}")
    return asyncio.run(run(args.data_folder))


if __name__ == "__main__":
    raise SystemExit(main())
