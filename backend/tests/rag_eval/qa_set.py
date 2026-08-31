"""Task 2.8 — the RAG test suite's fixed question set.

Every (question, expected_page) pair below was written AFTER reading the
actual chunk text for that page directly out of Pinecone (not guessed, not
copied from a table of contents) — each answer is genuinely present on the
stated page of Claude_Code_CampusX.pdf, the real document already uploaded
for bot 6a8d5a4c3bdff3d50a84021a.

Deliberately not all straightforward lookups (per the manual's own tip):
  - most are direct factual questions, phrased close to the source page
  - a handful are paraphrased into different words than the document uses,
    specifically to test whether embedding search finds the passage by
    MEANING rather than keyword overlap
  - three have expected_page=None: real questions this document genuinely
    has no answer to. The right behavior here isn't "find something close
    enough" — it's returning nothing, so the bot says it doesn't know
    rather than confidently answering from an unrelated passage.
"""

RAG_TEST_CASES = [
    # Direct lookups
    {"question": "What is Vibe Coding?", "expected_page": 18},
    {"question": "How do I authorize Claude Code the first time I use it?", "expected_page": 26},
    {"question": "How do I install Claude Code?", "expected_page": 30},
    {"question": "What are the three Claude models available and their prices?", "expected_page": 34},
    {"question": "What project does chapter 4 focus on improving?", "expected_page": 38},
    {"question": "What are the development workflow best practices?", "expected_page": 45},
    {"question": "How many tokens are usable out of the advertised 200k context window?", "expected_page": 49},
    {"question": "Where is the memory.md file stored?", "expected_page": 61},
    {"question": "What is Spec-Driven Development?", "expected_page": 65},
    {"question": "What columns are in the users table in the database schema?", "expected_page": 73},
    {"question": "What is the model selection table for Plan Mode? Which model is fastest?", "expected_page": 76},
    {"question": "What is the magic variable used inside custom slash commands?", "expected_page": 84},
    {"question": "What is a Skill's folder structure?", "expected_page": 91},
    {"question": "How do skills solve the problem of context window getting burned?", "expected_page": 95},
    {"question": "Why does the 'resend the whole conversation' hack exist for LLMs?", "expected_page": 99},
    {"question": "What are the two modes subagents get triggered by?", "expected_page": 103},
    {"question": "What are the two parts of a subagent markdown file?", "expected_page": 108},
    {"question": "What does the security-reviewer subagent check for?", "expected_page": 112},
    {"question": "What problems existed before MCP?", "expected_page": 116},
    {"question": "What permission must be enabled when creating a GitHub PAT for the MCP server?", "expected_page": 120},
    {"question": "What are the session lifecycle hook events?", "expected_page": 128},
    {"question": "What exit code should a hook use to block an action?", "expected_page": 135},
    {"question": "Why is calling dropna() without specifying columns a problem in a data science hook?", "expected_page": 139},
    {"question": "What's the difference between the official and third-party plugin marketplaces?", "expected_page": 143},
    {"question": "Why is Railway with SQLite risky for deployment?", "expected_page": 147},

    # Paraphrased — different words than the document itself uses
    {"question": "What's the downside of just letting an AI write all your code from a plain English description?", "expected_page": 18},
    {"question": "Does chat history size increase in a straight line or speed up as a session goes on?", "expected_page": 53},
    {"question": "What should you avoid touching in a database file according to the example rules?", "expected_page": 57},
    {"question": "How does the AI decide when to automatically use a specialized subagent on its own?", "expected_page": 103},

    # Not in the document — correct behavior is finding nothing
    {"question": "What is the price of a monthly subscription to CampusX's other courses?", "expected_page": None},
    {"question": "Who is the CEO of Anthropic?", "expected_page": None},
    {"question": "What's the weather forecast for this weekend?", "expected_page": None},
]

BOT_ID = "6a8d5a4c3bdff3d50a84021a"
