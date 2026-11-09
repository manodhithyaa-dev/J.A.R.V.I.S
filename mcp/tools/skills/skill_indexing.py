
#!/usr/bin/env python3

import argparse
import hashlib
import json
import logging
import re
import time

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup


SKILLS_URL = "https://skills.sh"
GITHUB_API = "https://api.github.com"
RAW_GITHUB = "https://raw.githubusercontent.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/131.0.0.0",
    "Accept": "text/html,application/xhtml+xml,application/json"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)

log = logging.getLogger("JARVIS-SKILL-INDEXER")


def now():
    return datetime.now(timezone.utc).isoformat()


def slugify(value):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    return value.strip("-") or "unnamed-skill"


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


class SkillIndexer:

    def __init__(self, output="./skill_library"):

        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)

        self.index_file = self.output / "index.json"

        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ==========================================
    # HTTP
    # ==========================================

    def get_html(self, url):

        response = self.session.get(
            url,
            timeout=30
        )

        response.raise_for_status()

        return BeautifulSoup(
            response.text,
            "html.parser"
        )

    def get_json(self, url):

        response = self.session.get(
            url,
            headers={
                "Accept": "application/vnd.github+json"
            },
            timeout=30
        )

        response.raise_for_status()

        return response.json()

    # ==========================================
    # 1. DISCOVER SKILLS FROM SKILLS.SH
    # ==========================================

    def discover(self, view="trending", pages=1):

        discovered = {}

        if view == "trending":
            base_url = f"{SKILLS_URL}/trending"

        elif view == "hot":
            base_url = f"{SKILLS_URL}/hot"

        else:
            base_url = SKILLS_URL

        for page in range(pages):

            url = (
                base_url if page == 0
                else f"{base_url}?page={page + 1}"
            )

            log.info("Scraping catalogue: %s", url)

            try:
                soup = self.get_html(url)

            except Exception as exc:
                log.error("Catalogue request failed: %s", exc)
                continue

            for anchor in soup.find_all("a", href=True):

                href = urljoin(url, anchor["href"])

                parsed = urlparse(href)

                if parsed.netloc not in (
                    "skills.sh",
                    "www.skills.sh"
                ):
                    continue

                parts = [
                    p for p in parsed.path.strip("/").split("/")
                    if p
                ]

                # Expected:
                # /owner/repository/skill-name

                if len(parts) != 3:
                    continue

                skill_id = "/".join(parts)

                discovered[skill_id] = {
                    "id": skill_id,
                    "name": clean(anchor.get_text()) or parts[2],
                    "owner": parts[0],
                    "repo": parts[1],
                    "slug": parts[2],
                    "skills_url": f"{SKILLS_URL}/{skill_id}"
                }

            time.sleep(0.3)

        log.info("Discovered %d skills", len(discovered))

        return list(discovered.values())

    # ==========================================
    # 2. SCRAPE INDIVIDUAL SKILLS.SH PAGE
    # ==========================================

    def get_skill_source(self, skill):

        log.info(
            "Inspecting skill page: %s",
            skill["skills_url"]
        )

        soup = self.get_html(skill["skills_url"])

        github_repo = None

        # Find GitHub repository link on the detail page.

        for anchor in soup.find_all("a", href=True):

            href = anchor["href"]

            if "github.com/" not in href:
                continue

            parsed = urlparse(href)

            parts = [
                p for p in parsed.path.strip("/").split("/")
                if p
            ]

            if len(parts) < 2:
                continue

            if parts[0] in ("settings", "login", "features"):
                continue

            github_repo = (
                f"https://github.com/{parts[0]}/{parts[1]}"
            )

            break

        # Fallback to repository shown in the skills.sh URL.
        # This is a source repository, not necessarily the original
        # upstream source mentioned elsewhere on the page.

        if not github_repo:
            github_repo = (
                f"https://github.com/"
                f"{skill['owner']}/{skill['repo']}"
            )

        skill["github_repo"] = github_repo

        log.info("GitHub repository: %s", github_repo)

        return skill

    # ==========================================
    # 3. FIND THE ORIGINAL SKILL.MD IN GITHUB
    # ==========================================

    def find_skill_file(self, skill):

        repo_url = skill["github_repo"]

        match = re.search(
            r"github\.com/([^/]+)/([^/?#]+)",
            repo_url
        )

        if not match:
            raise ValueError(
                f"Invalid GitHub repository URL: {repo_url}"
            )

        owner, repo = match.groups()

        repo = repo.removesuffix(".git")

        repo_data = self.get_json(
            f"{GITHUB_API}/repos/{owner}/{repo}"
        )

        branch = repo_data["default_branch"]

        log.info(
            "Inspecting GitHub tree: %s (%s)",
            repo,
            branch
        )

        tree_data = self.get_json(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/"
            f"{quote(branch, safe='')}"
            "?recursive=1"
        )

        if tree_data.get("truncated"):
            log.warning(
                "GitHub returned a truncated tree for %s",
                repo
            )

        files = tree_data.get("tree", [])

        candidates = []

        for item in files:

            if item.get("type") != "blob":
                continue

            path = item.get("path", "")

            if path.split("/")[-1].lower() != "skill.md":
                continue

            candidates.append(path)

        if not candidates:
            raise FileNotFoundError(
                f"No SKILL.md found in {owner}/{repo}"
            )

        slug = slugify(skill["slug"])

        # Prefer a SKILL.md inside a directory matching
        # the skill name.

        exact = []

        for path in candidates:

            parent = path.rsplit("/", 1)[0]

            directory = parent.split("/")[-1]

            if slugify(directory) == slug:
                exact.append(path)

        # Prefer conventional skills/ paths when there
        # are multiple matches.

        if exact:
            candidates = exact

        else:
            # Root SKILL.md is a valid candidate.
            root_files = [
                path for path in candidates
                if "/" not in path
            ]

            if root_files:
                candidates = root_files

            elif len(candidates) == 1:
                pass

            else:
                raise RuntimeError(
                    "Multiple SKILL.md files found, but none "
                    f"matches skill name '{slug}': {candidates}"
                )

        if len(candidates) > 1:

            raise RuntimeError(
                "Multiple matching SKILL.md files found. "
                f"Cannot choose safely: {candidates}"
            )

        skill_path = candidates[0]

        skill["github_owner"] = owner
        skill["github_repository"] = repo
        skill["github_branch"] = branch
        skill["github_file"] = skill_path

        log.info("Found original file: %s", skill_path)

        return skill

    # ==========================================
    # 4. DOWNLOAD RAW SKILL.MD
    # ==========================================

    def download_skill(self, skill):

        owner = skill["github_owner"]
        repo = skill["github_repository"]
        branch = skill["github_branch"]
        file_path = skill["github_file"]

        raw_url = (
            f"{RAW_GITHUB}/{owner}/{repo}/"
            f"{quote(branch, safe='')}/"
            f"{quote(file_path, safe='/')}"
        )

        log.info("Downloading original SKILL.md")

        response = self.session.get(
            raw_url,
            timeout=30
        )

        response.raise_for_status()

        content = response.content.decode(
            "utf-8-sig",
            errors="strict"
        )

        if not content.strip():
            raise ValueError("Downloaded SKILL.md is empty")

        if len(content) > 2_000_000:
            raise ValueError("SKILL.md exceeds 2 MB limit")

        skill["content"] = content
        skill["raw_url"] = raw_url

        return skill

    # ==========================================
    # 5. EXTRACT METADATA FROM ORIGINAL MARKDOWN
    # ==========================================

    def extract_metadata(self, content):

        metadata = {}

        if not content.startswith("---"):
            return metadata

        parts = content.split("---", 2)

        if len(parts) < 3:
            return metadata

        for line in parts[1].splitlines():

            match = re.match(
                r"^\s*([a-zA-Z_-]+)\s*:\s*(.*?)\s*$",
                line
            )

            if match:

                key, value = match.groups()

                metadata[key.lower()] = (
                    value.strip().strip('"').strip("'")
                )

        return metadata

    # ==========================================
    # 6. GENERATE SUMMARY AND CATEGORY
    # ==========================================

    def summarize(self, content, metadata):

        if metadata.get("description"):
            return clean(metadata["description"])

        lines = content.splitlines()

        for line in lines:

            line = clean(line)

            if line and not line.startswith("#") and line != "---":
                return line[:500]

        return "No description available."

    def categorize(self, name, summary, metadata):

        if metadata.get("category"):
            return slugify(metadata["category"])

        categories = {

            "coding-development": [
                "code", "python", "javascript",
                "programming", "development", "debug"
            ],

            "research": [
                "research", "academic", "paper",
                "citation", "literature"
            ],

            "ai-ml": [
                "ai", "machine learning", "llm",
                "agent", "model", "embedding"
            ],

            "web-browser": [
                "web", "browser", "website",
                "scraping", "search"
            ],

            "data-analytics": [
                "data", "analytics", "sql",
                "database", "statistics"
            ],

            "design": [
                "design", "figma", "ui",
                "ux", "image generation"
            ],

            "robotics-iot": [
                "robot", "robotics", "arduino",
                "esp32", "ros2", "iot"
            ],

            "security": [
                "security", "vulnerability",
                "audit", "privacy"
            ],

            "devops": [
                "docker", "kubernetes",
                "deployment", "linux", "cloud"
            ]
        }

        text = f"{name} {summary}".lower()

        scores = {
            category: sum(
                keyword in text for keyword in keywords
            )
            for category, keywords in categories.items()
        }

        best = max(scores, key=scores.get)

        return best if scores[best] else "general"

    # ==========================================
    # 7. SAVE SKILL LOCALLY
    # ==========================================

    def save_skill(self, skill):

        content = skill["content"]

        metadata = self.extract_metadata(content)

        name = (
            metadata.get("name")
            or skill["slug"]
        )

        summary = self.summarize(
            content,
            metadata
        )

        category = self.categorize(
            name,
            summary,
            metadata
        )

        directory = slugify(skill["slug"])

        skill_dir = self.output / directory

        skill_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        skill_file = skill_dir / "SKILL.md"

        # Write original GitHub content unchanged.
        skill_file.write_text(
            content,
            encoding="utf-8"
        )

        entry = {

            "id": skill["id"],

            "name": name,

            "summary": summary,

            "category": category,

            "repository": (
                f"{skill['github_owner']}/"
                f"{skill['github_repository']}"
            ),

            "github_url": skill["github_repo"],

            "raw_url": skill["raw_url"],

            "source_file": skill["github_file"],

            "skills_url": skill["skills_url"],

            "directory": directory,

            "path": f"{directory}/SKILL.md",

            "content_sha256": hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest(),

            "indexed_at": now()
        }

        return entry

    # ==========================================
    # 8. INDEX MANAGEMENT
    # ==========================================

    def load_index(self):

        if not self.index_file.exists():
            return {}

        data = json.loads(
            self.index_file.read_text(
                encoding="utf-8"
            )
        )

        return {
            item["id"]: item
            for item in data.get("skills", [])
        }

    def save_index(self, index):

        data = {

            "version": 1,

            "updated_at": now(),

            "total_skills": len(index),

            "skills": sorted(
                index.values(),
                key=lambda item: (
                    item.get("category", "general"),
                    item.get("name", "").lower()
                )
            )
        }

        temp = self.index_file.with_suffix(".tmp")

        temp.write_text(
            json.dumps(
                data,
                indent=2,
                ensure_ascii=False
            ),
            encoding="utf-8"
        )

        temp.replace(self.index_file)

    # ==========================================
    # 9. COMPLETE PIPELINE
    # ==========================================

    def run(self, view="trending", pages=1):

        skills = self.discover(view, pages)

        if not skills:
            log.error("No skills discovered.")
            return

        index = self.load_index()

        added = 0
        updated = 0
        failed = 0

        for skill in skills:

            try:

                # Scrape the skills.sh detail page.
                skill = self.get_skill_source(skill)

                # Locate original GitHub SKILL.md.
                skill = self.find_skill_file(skill)

                # Download original Markdown.
                skill = self.download_skill(skill)

                # Save locally and create metadata.
                entry = self.save_skill(skill)

                previous = index.get(entry["id"])

                index[entry["id"]] = entry

                if previous:
                    updated += 1
                    log.info("[UPDATED] %s", entry["path"])
                else:
                    added += 1
                    log.info("[ADDED] %s", entry["path"])

            except Exception as exc:

                failed += 1

                log.error(
                    "[FAILED] %s: %s",
                    skill.get("skills_url"),
                    exc
                )

            time.sleep(0.3)

        self.save_index(index)

        log.info("==============================")
        log.info("Added: %d", added)
        log.info("Updated: %d", updated)
        log.info("Failed: %d", failed)
        log.info("Total: %d", len(index))
        log.info("Index: %s", self.index_file)


# ==========================================
# CLI
# ==========================================

if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--view",
        choices=["trending", "hot", "all-time"],
        default="trending"
    )

    parser.add_argument(
        "--pages",
        type=int,
        default=1
    )

    parser.add_argument(
        "--output",
        default="./skill_library"
    )

    args = parser.parse_args()

    indexer = SkillIndexer(args.output)

    indexer.run(
        view=args.view,
        pages=args.pages
    )
