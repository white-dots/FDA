"""
Worker Agent — merged Librarian + Executor for remote code operations.

The Worker handles the technical side of client requests:
1. Receives task briefs from FDA via the message bus
2. SSHs into the correct Azure VM to read the codebase
3. Generates code fixes using Claude with full context
4. Creates diffs for FDA to send to the user for approval
5. Deploys approved changes to the VM

This replaces the separate Librarian and Executor agents from the
original FDA architecture, since for Datacore's use case, the agent
that reads the code is the same one that needs to change it.
"""

import json
import logging
import difflib
import re
import time
from typing import Any, Callable, Optional
from datetime import datetime

from fda.base_agent import BaseAgent
from fda.claude_backend import get_claude_backend
from fda.config import MODEL_EXECUTOR, MODEL_MEETING_SUMMARY
from fda.clients.client_config import ClientConfig, ClientManager
from fda.remote.ssh_manager import SSHManager
from fda.remote.deploy import Deployer, DeployResult
from fda.comms.message_bus import MessageBus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stopwords for keyword extraction (filtered out of task briefs)
# ---------------------------------------------------------------------------
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "their", "this", "that", "these", "those",
    "what", "which", "who", "whom", "where", "when", "how", "why",
    "and", "or", "but", "if", "then", "so", "because", "as", "of",
    "at", "by", "for", "with", "about", "against", "between", "through",
    "to", "from", "in", "on", "up", "out", "off", "over", "under",
    "not", "no", "nor", "only", "very", "too", "also", "just",
    "check", "look", "find", "show", "tell", "explain", "describe",
    "works", "work", "working", "make", "run", "use", "used",
})


class WorkerAgent(BaseAgent):
    """
    Technical execution agent for Datacore client operations.

    Combines code reading (Librarian) and code execution (Executor)
    into a single agent that operates on remote Azure VMs via SSH.
    """

    SYSTEM_PROMPT = """You are the Worker agent for Datacore, a software consultancy.
Your job is to make code changes on client Azure VMs based on task briefs from the FDA agent.

When you receive a task brief, you will:
1. Understand the client's request in business context
2. Read the relevant source files from their codebase
3. Identify what needs to change
4. Generate the minimal, correct code fix
5. Produce a clean diff showing exactly what changed

Important rules:
- Make minimal changes. Don't refactor unrelated code.
- Preserve the existing code style (indentation, naming conventions, etc.)
- If you're unsure about the fix, say so rather than guessing.
- Always explain what you changed and why in plain language.
- Consider edge cases and potential side effects.
- Never change database schemas without explicit approval.
- Never delete data or drop tables.
"""

    def __init__(
        self,
        client_manager: ClientManager,
        message_bus: Optional[MessageBus] = None,
        db_path: Optional[str] = None,
    ):
        """
        Initialize the Worker agent.

        Args:
            client_manager: Manager for client configs.
            message_bus: Inter-agent message bus.
            db_path: Path to SQLite database.
        """
        super().__init__(
            name="worker",
            model=MODEL_EXECUTOR,
            system_prompt=self.SYSTEM_PROMPT,
            message_bus=message_bus,
            db_path=db_path,
        )
        self.client_manager = client_manager
        self._backend = get_claude_backend()

        # Cache SSH connections per client
        self._ssh_connections: dict[str, SSHManager] = {}
        self._deployers: dict[str, Deployer] = {}

        # Cache for codebase file listings (avoids re-running `find` every call)
        # Key: (client_id, repo_path), Value: (timestamp, file_list)
        self._structure_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
        self._structure_cache_ttl = 300.0  # 5 minutes

        # Cache for file importance rankings (git-based, refreshed daily)
        # Key: client_id, Value: (timestamp, {filepath: importance_score})
        self._ranking_cache: dict[str, tuple[float, dict[str, float]]] = {}
        self._ranking_cache_ttl = 86400.0  # 24 hours

        # Warm up SSH connections to all clients at init time
        self._warmup_connections()

    def _get_ssh(self, client: ClientConfig) -> SSHManager:
        """Get or create SSH connection for a client."""
        if client.client_id not in self._ssh_connections:
            self._ssh_connections[client.client_id] = SSHManager(
                host=client.vm.host,
                user=client.vm.ssh_user,
                ssh_key=client.vm.ssh_key,
                port=client.vm.port,
            )
        return self._ssh_connections[client.client_id]

    def _get_deployer(self, client: ClientConfig) -> Deployer:
        """Get or create deployer for a client."""
        if client.client_id not in self._deployers:
            self._deployers[client.client_id] = Deployer(client)
        return self._deployers[client.client_id]

    def _warmup_connections(self) -> None:
        """Proactively establish SSH ControlMaster connections to all clients.

        Called at init time so the first analyze_and_fix() call doesn't pay
        the ~1.5s TCP+SSH handshake cost. The ControlMaster persists for
        10 minutes of inactivity, and subsequent SSH commands multiplex
        over it (~50ms instead of ~1.5s).
        """
        for client in self.client_manager.list_clients():
            try:
                ssh = self._get_ssh(client)
                if ssh.warmup():
                    logger.info(
                        f"SSH connection warmed up for {client.name} "
                        f"({client.vm.host})"
                    )
                else:
                    logger.warning(
                        f"Failed to warm up SSH for {client.name} "
                        f"({client.vm.host})"
                    )
            except Exception as e:
                logger.warning(f"SSH warmup error for {client.name}: {e}")

    def analyze_and_fix(
        self,
        client_id: str,
        task_brief: str,
        hint_files: Optional[list[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """
        Analyze a client request and generate a code fix.

        This is the main entry point for the Worker.

        Args:
            client_id: Client identifier.
            task_brief: Business-context task description from FDA.
            hint_files: Optional list of file paths to examine first.
            progress_callback: Optional callback for live progress updates.
                Called with a status string at each step. Thread-safe.

        Returns:
            Dict with:
              - success: bool
              - analysis: str (what the agent found)
              - changes: dict[str, str] (file path -> new content)
              - diff: str (unified diff of all changes)
              - explanation: str (human-readable explanation)
              - error: Optional[str]
        """
        def _progress(msg: str) -> None:
            """Send a progress update (to callback + logger)."""
            logger.info(f"[Worker] {msg}")
            if progress_callback:
                try:
                    progress_callback(msg)
                except Exception:
                    pass  # Never let callback errors break the pipeline

        client = self.client_manager.get_client(client_id)
        if not client:
            return {
                "success": False,
                "error": f"Unknown client: {client_id}",
            }

        ssh = self._get_ssh(client)
        repo_path = client.project.repo_path

        # Step 1: Understand the codebase structure
        all_paths = [repo_path] + (client.project.extra_repo_paths or [])
        path_label = f" across {len(all_paths)} paths" if len(all_paths) > 1 else ""
        _progress(f"📂 Scanning codebase on {client.name} VM...")
        structure = self._explore_codebase(ssh, repo_path, client)
        _progress(f"📂 Found {len(structure)} files{path_label}")

        # Step 2: Identify relevant files
        _progress("🔍 Identifying relevant files...")
        relevant_files = self._identify_relevant_files(
            task_brief, structure, hint_files, client
        )

        if not relevant_files:
            _progress("❌ No relevant files found")
            return {
                "success": False,
                "error": "Could not identify relevant files for this task",
                "analysis": f"Explored {len(structure)} files but none matched the request",
            }

        # Show which files were selected
        short_names = [f.rsplit("/", 1)[-1] for f in relevant_files[:5]]
        extras = f" +{len(relevant_files) - 5} more" if len(relevant_files) > 5 else ""
        _progress(f"🔍 Found {len(relevant_files)} relevant files: {', '.join(short_names)}{extras}")

        # Step 3: Read the relevant source files
        _progress(f"📖 Reading {len(relevant_files)} files from VM...")

        # Resolve relative paths (from `find .`) to absolute paths
        # so read_files() can cat them without needing cwd.
        # Also expand ~ to $HOME since cat "~/..." won't expand tilde
        # inside quotes on the remote shell.
        def _resolve(f: str) -> str:
            if f.startswith("./"):
                return f"{repo_path}/{f[2:]}"
            elif f.startswith("~") or f.startswith("/"):
                return f  # already absolute (from extra paths or hint_files)
            else:
                return f"{repo_path}/{f}"

        abs_files = [_resolve(f) for f in relevant_files]

        # Expand ~ → $HOME on the remote host (tilde doesn't expand inside quotes)
        if any("~" in f for f in abs_files):
            home_result = ssh.execute("echo $HOME")
            remote_home = home_result.stdout.strip() if home_result.success else "/home"
            abs_files = [f.replace("~", remote_home) for f in abs_files]

        file_contents = ssh.read_files(abs_files)

        # Re-map keys back to the original relative paths for downstream use
        file_contents = {
            rel: file_contents.get(absl)
            for rel, absl in zip(relevant_files, abs_files)
        }

        # Filter out files that couldn't be read
        readable_files = {
            path: content
            for path, content in file_contents.items()
            if content is not None
        }

        if not readable_files:
            _progress("❌ Could not read any files")
            return {
                "success": False,
                "error": "Could not read any of the relevant files",
            }

        total_chars = sum(len(c) for c in readable_files.values())
        _progress(f"📖 Read {len(readable_files)} files ({total_chars:,} chars)")

        # Step 4: Generate the fix using Claude
        _progress("🧠 Analyzing with Claude...")
        fix_result = self._generate_fix(
            task_brief=task_brief,
            client=client,
            file_contents=readable_files,
            repo_path=repo_path,
        )

        if fix_result.get("success"):
            _progress("✅ Analysis complete")
        else:
            _progress(f"⚠️ Analysis finished with errors: {fix_result.get('error', 'unknown')[:100]}")

        return fix_result

    def _explore_codebase(
        self,
        ssh: SSHManager,
        repo_path: str,
        client: ClientConfig,
    ) -> list[str]:
        """
        Get an overview of the codebase structure across all repo paths.

        Scans ``repo_path`` plus any ``extra_repo_paths`` from the client
        config.  Each path is cached independently (5-min TTL).  After
        collecting all files, they are sorted by importance score (most
        important first) using git-based ranking (24h TTL cache).

        Returns a list of file paths sorted by importance.
        """
        all_paths = [repo_path] + (client.project.extra_repo_paths or [])
        combined_files: list[str] = []
        now = time.monotonic()

        _find_cmd = (
            "find . -type f "
            "! -path './.git/*' "
            "! -path './node_modules/*' "
            "! -path './__pycache__/*' "
            "! -path './venv/*' "
            "! -path './.venv/*' "
            "! -path './env/*' "
            "! -path './.env/*' "
            "! -path './dist/*' "
            "! -path './build/*' "
            "! -path './*.pyc' "
            "| head -2000"
        )

        for path in all_paths:
            cache_key = (client.client_id, path)

            # Check cache
            if cache_key in self._structure_cache:
                cached_time, cached_files = self._structure_cache[cache_key]
                if (now - cached_time) < self._structure_cache_ttl:
                    combined_files.extend(cached_files)
                    continue

            result = ssh.execute(_find_cmd, cwd=path, timeout=15)

            if result.success and result.stdout.strip():
                files = [
                    line.strip()
                    for line in result.stdout.strip().split("\n")
                    if line.strip()
                ]
                # Prefix files from extra paths with their root so they
                # can be resolved to absolute paths downstream.
                if path != repo_path:
                    files = [
                        f"{path}/{f[2:]}" if f.startswith("./") else f"{path}/{f}"
                        for f in files
                    ]
                self._structure_cache[cache_key] = (now, files)
                logger.info(f"Cached file listing for {client.name} ({path}): {len(files)} files")
                combined_files.extend(files)

        # Rank files by importance (git signals + query history)
        rankings = self._rank_files(ssh, client, all_paths)
        if rankings:
            combined_files.sort(key=lambda f: rankings.get(f, 0.0), reverse=True)

        return combined_files

    def invalidate_structure_cache(self, client_id: Optional[str] = None) -> None:
        """Clear the codebase structure cache.

        Args:
            client_id: If provided, only clear cache for this client.
                       If None, clear all cached structures.
        """
        if client_id:
            keys_to_remove = [k for k in self._structure_cache if k[0] == client_id]
            for key in keys_to_remove:
                del self._structure_cache[key]
            logger.debug(f"Cleared structure cache for {client_id}")
        else:
            self._structure_cache.clear()
            logger.debug("Cleared all structure caches")

    # ------------------------------------------------------------------
    # File ranking — git signals + query history
    # ------------------------------------------------------------------

    def _rank_files(
        self,
        ssh: SSHManager,
        client: ClientConfig,
        search_paths: Optional[list[str]] = None,
    ) -> dict[str, float]:
        """Compute importance scores for all files using git or filesystem signals.

        When git is available:
          - Recent edits in last 3 months via git log (0.30)
          - All-time commit frequency via git log   (0.25)
        When git is NOT available (fallback):
          - Recently modified files via find -mtime  (0.30)
          - File complexity via wc -l                (0.25)

        Always:
          - Python import hub score via grep         (0.15)
          - Query history from state DB              (0.30)

        Results are cached per client for 24 hours.
        """
        now = time.monotonic()
        if client.client_id in self._ranking_cache:
            cached_time, cached_ranks = self._ranking_cache[client.client_id]
            if (now - cached_time) < self._ranking_cache_ttl:
                return cached_ranks

        if search_paths is None:
            search_paths = [client.project.repo_path] + (client.project.extra_repo_paths or [])

        # Collect raw counts per signal across all paths
        recent: dict[str, int] = {}   # filepath → recency score
        frequency: dict[str, int] = {}  # filepath → frequency/complexity score
        imports: dict[str, int] = {}   # module name → import count

        # Check if first path has git (use test -d to avoid SSH warning on non-git repos)
        git_check = ssh.execute('test -d .git && echo yes || echo no',
                                cwd=search_paths[0], timeout=5)
        has_git = git_check.success and git_check.stdout.strip() == 'yes'

        if has_git:
            batch_cmd = (
                '('
                'echo "===RECENT==="; '
                'git log --all --pretty=format: --name-only --since="3 months ago" 2>/dev/null '
                '| grep -v "^$" | sort | uniq -c | sort -rn | head -200; '
                'echo "===FREQUENCY==="; '
                'git log --all --pretty=format: --name-only 2>/dev/null '
                '| grep -v "^$" | sort | uniq -c | sort -rn | head -200; '
                'echo "===IMPORTS==="; '
                'grep -rh "^from \\|^import " --include="*.py" . 2>/dev/null '
                "| sed 's/from \\([^ ]*\\).*/\\1/; s/import \\([^ ,]*\\).*/\\1/' "
                '| sort | uniq -c | sort -rn | head -100'
                ')'
            )
        else:
            # Filesystem-based fallback: mtime for recency, line count for complexity
            excl = (
                '! -path "*/node_modules/*" ! -path "*/__pycache__/*" '
                '! -path "*/.git/*" ! -path "*/.ipynb_checkpoints/*" '
                '! -path "*/backup*/*"'
            )
            batch_cmd = (
                '('
                'echo "===RECENT==="; '
                'find . -type f \\( -name "*.py" -o -name "*.sql" -o -name "*.yaml" -o -name "*.yml" '
                '-o -name "*.sh" -o -name "*.cfg" -o -name "*.conf" \\) '
                f'{excl} '
                '-mtime -90 -printf "%T@ %p\\n" 2>/dev/null '
                '| sort -rn | head -200 | awk \'{print NR, $2}\'; '
                'echo "===FREQUENCY==="; '
                f'find . -type f -name "*.py" {excl} -exec wc -l {{}} + 2>/dev/null '
                '| grep -v " total$" | sort -rn | head -200; '
                'echo "===IMPORTS==="; '
                'grep -rh "^from \\|^import " --include="*.py" '
                '--exclude-dir=node_modules --exclude-dir=__pycache__ . 2>/dev/null '
                "| sed 's/from \\([^ ]*\\).*/\\1/; s/import \\([^ ,]*\\).*/\\1/' "
                '| sort | uniq -c | sort -rn | head -100'
                ')'
            )

        for path in search_paths:
            result = ssh.execute(batch_cmd, cwd=path, timeout=30)
            if not result.success:
                continue

            section = None
            is_primary = (path == search_paths[0])

            for line in result.stdout.split("\n"):
                stripped = line.strip()
                if stripped == "===RECENT===":
                    section = "recent"
                    continue
                elif stripped == "===FREQUENCY===":
                    section = "frequency"
                    continue
                elif stripped == "===IMPORTS===":
                    section = "imports"
                    continue

                if not stripped or section is None:
                    continue

                # Lines look like: "  42 path/to/file.py"
                parts = stripped.split(None, 1)
                if len(parts) != 2:
                    continue

                try:
                    count = int(parts[0])
                except ValueError:
                    continue

                name = parts[1]
                # Strip leading "./" from find output
                if name.startswith("./"):
                    name = name[2:]

                # Prefix files from extra paths
                if not is_primary and section in ("recent", "frequency"):
                    name = f"{path}/{name}"

                if section == "recent":
                    recent[name] = recent.get(name, 0) + count
                elif section == "frequency":
                    frequency[name] = frequency.get(name, 0) + count
                elif section == "imports":
                    imports[name] = imports.get(name, 0) + count

        # Query history from state DB
        query_counts: dict[str, int] = {}
        try:
            from fda.state.project_state import ProjectState
            state = ProjectState()
            for fp, cnt in state.get_top_files_by_relevance(client.client_id, limit=100):
                query_counts[fp] = cnt
        except Exception:
            pass  # state DB may not be available

        # Normalize each signal and combine
        def _max_or_1(d: dict) -> float:
            return max(d.values()) if d else 1.0

        max_recent = _max_or_1(recent)
        max_freq = _max_or_1(frequency)
        max_imports = _max_or_1(imports)
        max_query = _max_or_1(query_counts)

        all_files = set(recent) | set(frequency) | set(query_counts)
        scores: dict[str, float] = {}

        for f in all_files:
            s = 0.0
            s += (recent.get(f, 0) / max_recent) * 0.30
            s += (frequency.get(f, 0) / max_freq) * 0.25
            s += (query_counts.get(f, 0) / max_query) * 0.30
            # Import score: match module names to file paths
            # e.g. imports["utils.helpers"] matches file "./utils/helpers.py"
            for mod, cnt in imports.items():
                mod_path = mod.replace(".", "/")
                if mod_path in f:
                    s += (cnt / max_imports) * 0.15
                    break
            scores[f] = s

        mode = "git" if has_git else "filesystem"
        self._ranking_cache[client.client_id] = (now, scores)
        logger.info(
            f"Ranked {len(scores)} files for {client.name} ({mode} mode, "
            f"recent={len(recent)}, freq={len(frequency)}, "
            f"imports={len(imports)}, queries={len(query_counts)})"
        )
        return scores

    # ------------------------------------------------------------------
    # Keyword extraction + remote grep
    # ------------------------------------------------------------------

    def _extract_search_keywords(
        self, task_brief: str, client: ClientConfig
    ) -> list[str]:
        """Extract meaningful search keywords from task brief."""
        words = set()
        for word in re.split(r"[\s,;:!?.\"'()\[\]{}/]+", task_brief.lower()):
            if word and len(word) > 2 and word not in _STOPWORDS:
                words.add(word)
                # Add singular/plural variants so "dags" also matches "dag"
                if word.endswith("s") and len(word) > 4:
                    words.add(word[:-1])
                elif not word.endswith("s"):
                    words.add(word + "s")

        # Add compound pairs for adjacent keywords (e.g. brand+sales)
        word_list = [
            w for w in re.split(r"[\s,;:!?.\"'()\[\]{}/]+", task_brief.lower())
            if w and len(w) > 2 and w not in _STOPWORDS
        ]
        for i in range(len(word_list) - 1):
            a, b = word_list[i], word_list[i + 1]
            words.add(f"{a}_{b}")
            words.add(f"{a}.*{b}")  # regex-friendly for grep

        return list(words)

    def _grep_remote_files(
        self,
        ssh: SSHManager,
        keywords: list[str],
        search_paths: list[str],
    ) -> list[str]:
        """Use remote ``grep -rl`` to find files containing keywords."""
        if not keywords:
            return []

        # Pick the most specific keywords (longer = more specific)
        sorted_kw = sorted(keywords, key=len, reverse=True)[:15]
        pattern = "|".join(sorted_kw)

        grep_files: list[str] = []
        seen: set[str] = set()

        for path in search_paths:
            result = ssh.execute(
                f'grep -rl -i --include="*.py" --include="*.sql" '
                f'--include="*.yaml" --include="*.yml" --include="*.cfg" '
                f'--include="*.conf" --include="*.sh" '
                f'--exclude-dir=node_modules --exclude-dir=__pycache__ '
                f'--exclude-dir=.git --exclude-dir=.ipynb_checkpoints '
                f'-E "{pattern}" . 2>/dev/null | head -100',
                cwd=path,
                timeout=15,
            )
            if not result.success or not result.stdout.strip():
                continue

            is_primary = (path == search_paths[0])
            for line in result.stdout.strip().split("\n"):
                f = line.strip()
                if not f:
                    continue
                if not is_primary:
                    f = f"{path}/{f[2:]}" if f.startswith("./") else f"{path}/{f}"
                if f not in seen:
                    seen.add(f)
                    grep_files.append(f)

        return grep_files

    # ------------------------------------------------------------------
    # File identification — 3-tier strategy
    # ------------------------------------------------------------------

    def _identify_relevant_files(
        self,
        task_brief: str,
        all_files: list[str],
        hint_files: Optional[list[str]],
        client: ClientConfig,
    ) -> list[str]:
        """
        Identify files relevant to the task using a three-tier strategy:

        1. If *hint_files* provided, use those directly.
        2. Extract keywords → ``grep`` remote VM for content matches.
        3. Send grep hits + **all** filenames to Claude for final selection.
        4. Fallback to improved heuristic if Claude fails.

        After selection, records query history in the state DB so the
        "most queried" ranking signal improves over time.
        """
        if hint_files:
            return hint_files

        ssh = self._get_ssh(client)
        search_paths = [client.project.repo_path] + (client.project.extra_repo_paths or [])

        # --- Stage 1: keyword extraction + remote grep ---
        keywords = self._extract_search_keywords(task_brief, client)
        grep_hits = self._grep_remote_files(ssh, keywords, search_paths)
        if grep_hits:
            logger.info(f"Grep found {len(grep_hits)} content-matching files")

        # --- Stage 2: Claude selection ---
        grep_section = ""
        if grep_hits:
            grep_section = (
                "FILES CONTAINING RELEVANT KEYWORDS (high priority — these "
                "files actually contain terms from the task):\n"
                + "\n".join(grep_hits[:50])
                + "\n\n"
            )

        # Send top-ranked files + grep hits (not all 4000 — that exceeds token budget)
        # Grep hits are highest priority, then ranked files fill the rest
        file_set = set(grep_hits)
        ordered = list(grep_hits)
        for f in all_files:
            if f not in file_set:
                file_set.add(f)
                ordered.append(f)
            if len(ordered) >= 800:
                break
        files_list = "\n".join(ordered)

        try:
            text = self._backend.complete(
                system=(
                    "You are a code analyst. Given a task description and "
                    "file listings, identify which files are relevant to the "
                    "task. Return ONLY a JSON array of file paths, nothing else.\n\n"
                    "IMPORTANT: Be INCLUSIVE, not exclusive. When the task asks "
                    "about a category (e.g. 'all DAGs', 'all scripts', 'all "
                    "configs'), include ALL files that belong to that category. "
                    "When in doubt, include the file — it's better to include "
                    "a few extra files than to miss relevant ones."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        f"Task: {task_brief}\n\n"
                        f"Tech stack: {client.project.tech_stack}\n"
                        f"Business context: {client.business_context[:300]}\n\n"
                        f"{grep_section}"
                        f"ALL REPOSITORY FILES:\n{files_list}\n\n"
                        "Which files should I read to understand and address "
                        "this task?\n"
                        "Return a JSON array of ALL relevant file paths "
                        "(up to 50 files). Include every file that could be "
                        "relevant — err on the side of inclusion.\n"
                        "Prioritize files from the keyword-matching section "
                        "if present."
                    ),
                }],
                model=MODEL_EXECUTOR,
                max_tokens=4000,
            )

            text = text.strip()
            # Strip markdown code blocks if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            # Try direct parse first, then extract JSON array from mixed text
            files = None
            try:
                files = json.loads(text)
            except json.JSONDecodeError:
                # Claude often adds explanation text around the JSON array —
                # extract the first [...] block from the response
                match = re.search(r'\[.*\]', text, re.DOTALL)
                if match:
                    try:
                        files = json.loads(match.group())
                    except json.JSONDecodeError:
                        pass
            if isinstance(files, list) and files:
                known = set(all_files) | set(grep_hits)
                valid = [f for f in files if f in known]
                if valid:
                    self._record_file_access(client.client_id, valid)
                    return valid
                else:
                    logger.warning(
                        f"File identification returned {len(files)} files but "
                        f"none matched known files (sample: {files[:3]})"
                    )
        except Exception as e:
            logger.warning(f"File identification Claude call failed: {e}")

        # --- Stage 3: improved heuristic fallback ---
        fallback = self._heuristic_file_search(
            task_brief, all_files, grep_hits=grep_hits, client=client
        )
        if fallback:
            self._record_file_access(client.client_id, fallback)
        return fallback

    def _record_file_access(self, client_id: str, file_paths: list[str]) -> None:
        """Record selected files in the state DB for query-history ranking."""
        try:
            from fda.state.project_state import ProjectState
            state = ProjectState()
            state.increment_file_relevance(client_id, file_paths)
        except Exception:
            pass  # non-critical

    def _heuristic_file_search(
        self,
        task_brief: str,
        all_files: list[str],
        grep_hits: Optional[list[str]] = None,
        client: Optional[ClientConfig] = None,
    ) -> list[str]:
        """Improved fallback file search with stopword filtering and grep boost."""
        keywords = (
            self._extract_search_keywords(task_brief, client)
            if client
            else [w for w in task_brief.lower().split() if len(w) > 2 and w not in _STOPWORDS]
        )
        # Filter out regex-style compound keywords for filepath matching
        path_keywords = [kw for kw in keywords if ".*" not in kw]

        grep_set = set(grep_hits or [])
        rankings = self._ranking_cache.get(
            client.client_id, (0, {})
        )[1] if client else {}

        # File extensions to skip in heuristic (log files, data, images, etc.)
        _JUNK_EXTENSIONS = frozenset({
            ".log", ".bak", ".tmp", ".swp", ".pyc", ".pyo",
            ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico",
            ".zip", ".tar", ".gz", ".whl", ".egg",
            ".csv", ".tsv", ".parquet", ".pkl",
        })

        scored: list[tuple[str, float]] = []

        for filepath in set(all_files) | grep_set:
            fp_lower = filepath.lower()

            # Skip junk files
            ext = "." + fp_lower.rsplit(".", 1)[-1] if "." in fp_lower else ""
            if ext in _JUNK_EXTENSIONS:
                continue

            score = 0.0

            for kw in path_keywords:
                if kw in fp_lower:
                    score += 2.0

            if filepath in grep_set:
                score += 5.0

            # Boost from git-based importance
            score += rankings.get(filepath, 0.0) * 3.0

            if fp_lower.endswith((".py", ".sql")):
                score += 1.0

            if score > 0:
                scored.append((filepath, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [path for path, _ in scored[:50]]

    def _generate_fix(
        self,
        task_brief: str,
        client: ClientConfig,
        file_contents: dict[str, str],
        repo_path: str,
    ) -> dict[str, Any]:
        """
        Generate a code fix using Claude.

        Args:
            task_brief: Business-context task description.
            client: Client configuration.
            file_contents: Dict of file path -> file content.
            repo_path: Repository root path on the VM.

        Returns:
            Dict with success, changes, diff, and explanation.
        """
        # Build the source code context
        source_context = ""
        for path, content in file_contents.items():
            # Truncate very large files
            if len(content) > 10000:
                content = content[:10000] + "\n... [truncated] ..."
            source_context += f"\n=== {path} ===\n{content}\n"

        prompt = f"""You are working on a client project.

{client.get_context_for_prompt()}

TASK:
{task_brief}

SOURCE CODE:
{source_context}

INSTRUCTIONS:
1. Analyze the codebase in the context of the task
2. If the task requires code changes, make the MINIMAL change needed -- don't refactor unrelated code
3. If the task is an investigation or question (e.g. "check if X is configured", "does Y exist"), answer the question based on what you see in the code -- do NOT make any changes
4. Preserve the existing code style

RESPONSE FORMAT:
You MUST return a JSON object (no markdown, no extra text). Use this format:
{{
  "analysis": "Brief analysis of what you found",
  "explanation": "Plain-language explanation of your findings or what you changed and why",
  "changes": {{
    "relative/path/to/file.py": "FULL new content of the file (not just the changed part)"
  }},
  "confidence": "high|medium|low",
  "warnings": ["any concerns or things to check"]
}}

IMPORTANT:
- If no code changes are needed (investigation/query tasks), set "changes" to an empty object {{}}.
- If code changes are needed, include the COMPLETE file content in "changes", not just the diff.
- Only include files that actually changed.
- Your response must be valid JSON. Do not wrap it in markdown code blocks.
"""

        try:
            text = self._backend.complete(
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                model=MODEL_MEETING_SUMMARY,  # Use Sonnet for quality code generation
                max_tokens=8000,
            ).strip()

            # Extract JSON from response
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            result = json.loads(text)

            # Generate unified diff
            diff_parts = []
            changes = result.get("changes", {})

            for filepath, new_content in changes.items():
                old_content = file_contents.get(filepath, "")
                diff = difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    new_content.splitlines(keepends=True),
                    fromfile=f"a/{filepath}",
                    tofile=f"b/{filepath}",
                )
                diff_parts.append("".join(diff))

            is_investigation = not changes
            return {
                "success": True,
                "investigation": is_investigation,
                "analysis": result.get("analysis", ""),
                "changes": changes,
                "diff": "\n".join(diff_parts),
                "explanation": result.get("explanation", ""),
                "confidence": result.get("confidence", "unknown"),
                "warnings": result.get("warnings", []),
            }

        except json.JSONDecodeError as e:
            raw = text if 'text' in dir() else ""
            if raw:
                logger.info(
                    f"Claude returned non-JSON response ({len(raw)} chars) — "
                    "treating as investigation result"
                )
                return {
                    "success": True,
                    "investigation": True,
                    "analysis": raw,
                    "changes": {},
                    "diff": "",
                    "explanation": raw,
                    "confidence": "medium",
                    "warnings": [],
                }
            logger.error(f"Failed to parse Claude's response as JSON: {e}")
            return {
                "success": False,
                "error": f"Failed to parse code fix response: {e}",
                "raw_response": raw,
            }
        except Exception as e:
            logger.error(f"Error generating fix: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    def deploy_approved_changes(
        self,
        client_id: str,
        file_changes: dict[str, str],
    ) -> DeployResult:
        """
        Deploy approved code changes to a client's VM.

        Called after FDA receives user approval via Telegram.

        Args:
            client_id: Client identifier.
            file_changes: Dict of relative file paths -> new content.

        Returns:
            DeployResult with status and details.
        """
        client = self.client_manager.get_client(client_id)
        if not client:
            return DeployResult(
                success=False,
                client_id=client_id,
                error=f"Unknown client: {client_id}",
            )

        deployer = self._get_deployer(client)
        result = deployer.deploy_files(file_changes)

        # Invalidate structure cache after deploy (files may have changed)
        if result.success:
            self.invalidate_structure_cache(client_id)

        return result

    def test_all_connections(self) -> dict[str, dict]:
        """
        Test SSH connectivity to all client VMs.

        Returns:
            Dict mapping client_id -> connection status.
        """
        results = {}
        for client in self.client_manager.list_clients():
            deployer = self._get_deployer(client)
            results[client.client_id] = deployer.test_connectivity()
        return results

    # Message bus integration

    def handle_task_request(self, message: dict) -> None:
        """
        Handle a task request from FDA via the message bus.

        Expected message body:
        {
            "client_id": "client_a",
            "task_brief": "Client A wants...",
            "hint_files": ["path/to/file.py"],  # optional
        }
        """
        body = json.loads(message.get("body", "{}"))
        client_id = body.get("client_id")
        task_brief = body.get("task_brief")
        hint_files = body.get("hint_files")

        if not client_id or not task_brief:
            logger.error("Invalid task request: missing client_id or task_brief")
            return

        # Analyze and generate fix
        result = self.analyze_and_fix(client_id, task_brief, hint_files)

        # Send result back to FDA
        if self.message_bus:
            self.message_bus.send(
                from_agent="worker",
                to_agent="fda",
                msg_type="TASK_RESULT",
                subject=f"Fix for {client_id}",
                body=json.dumps(result),
                reply_to=message.get("id"),
            )

    def handle_deploy_request(self, message: dict) -> None:
        """
        Handle a deploy request from FDA (after user approval).

        Expected message body:
        {
            "client_id": "client_a",
            "file_changes": {"path/to/file.py": "new content..."}
        }
        """
        body = json.loads(message.get("body", "{}"))
        client_id = body.get("client_id")
        file_changes = body.get("file_changes", {})

        if not client_id or not file_changes:
            logger.error("Invalid deploy request")
            return

        result = self.deploy_approved_changes(client_id, file_changes)

        # Send result back to FDA
        if self.message_bus:
            self.message_bus.send(
                from_agent="worker",
                to_agent="fda",
                msg_type="DEPLOY_RESULT",
                subject=f"Deploy {'OK' if result.success else 'FAILED'} for {client_id}",
                body=json.dumps({
                    "success": result.success,
                    "summary": result.summary(),
                    "error": result.error,
                    "rolled_back": result.rolled_back,
                }),
                reply_to=message.get("id"),
            )

    def run_event_loop(self) -> None:
        """
        Main event loop — listen for task and deploy requests from FDA.
        """
        import time

        logger.info("Worker agent started, listening for requests...")

        if self.state:
            self.state.update_agent_status("worker", "running")

        while True:
            try:
                if self.message_bus:
                    messages = self.message_bus.get_pending("worker")
                    for msg in messages:
                        msg_type = msg.get("type", "")
                        self.message_bus.mark_read(msg["id"])

                        if msg_type == "TASK_REQUEST":
                            self.handle_task_request(msg)
                        elif msg_type == "DEPLOY_REQUEST":
                            self.handle_deploy_request(msg)
                        elif msg_type == "STATUS_REQUEST":
                            self._handle_status_request(msg)
                        else:
                            logger.debug(f"Ignoring message type: {msg_type}")

                    if self.state:
                        self.state.agent_heartbeat("worker")

                time.sleep(2)  # Check every 2 seconds

            except KeyboardInterrupt:
                logger.info("Worker agent shutting down...")
                if self.state:
                    self.state.update_agent_status("worker", "stopped")
                break
            except Exception as e:
                logger.error(f"Error in worker event loop: {e}", exc_info=True)
                time.sleep(5)

    def _handle_status_request(self, message: dict) -> None:
        """Respond to a status request."""
        connections = self.test_all_connections()
        if self.message_bus:
            self.message_bus.send(
                from_agent="worker",
                to_agent=message.get("from", "fda"),
                msg_type="STATUS_RESPONSE",
                subject="Worker status",
                body=json.dumps({
                    "status": "running",
                    "connections": connections,
                    "timestamp": datetime.now().isoformat(),
                }),
                reply_to=message.get("id"),
            )
