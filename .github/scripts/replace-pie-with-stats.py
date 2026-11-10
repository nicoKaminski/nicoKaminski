#!/usr/bin/env python3
"""Replace the language pie in the night-rainbow SVG with live GitHub stats."""

import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


API_URL = "https://api.github.com/graphql"
SVG_PATH = Path("profile-3d-contrib/profile-night-rainbow.svg")
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

PROFILE_QUERY = """
query ProfileStats($login: String!) {
  user(login: $login) {
    contributionsCollection { contributionYears }
    repositoriesContributedTo(
      first: 1,
      includeUserRepositories: true,
      privacy: PUBLIC,
      contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]
    ) { totalCount }
    repositories(
      first: 100,
      privacy: PUBLIC,
      isFork: false,
      ownerAffiliations: OWNER
    ) {
      nodes { stargazerCount }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

REPOSITORY_PAGE_QUERY = """
query RepositoryStars($login: String!, $cursor: String!) {
  user(login: $login) {
    repositories(
      first: 100,
      privacy: PUBLIC,
      isFork: false,
      ownerAffiliations: OWNER,
      after: $cursor
    ) {
      nodes { stargazerCount }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

YEAR_STATS_QUERY = """
query YearStats($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
    }
  }
}
"""


def graphql(token, query, variables):
    request = urllib.request.Request(
        API_URL,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "nicoKaminski-profile-stats",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub GraphQL request failed with HTTP {error.code}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"GitHub GraphQL request failed: {error.reason}") from None

    errors = payload.get("errors") or []
    if errors:
        messages = "; ".join(item.get("message", "unknown GraphQL error") for item in errors)
        raise RuntimeError(f"GitHub GraphQL returned an error: {messages}")

    user = (payload.get("data") or {}).get("user")
    if not user:
        raise RuntimeError("GitHub did not return profile data for the configured username")
    return user


def fetch_stats(token, username):
    profile = graphql(token, PROFILE_QUERY, {"login": username})
    repositories = profile["repositories"]
    stars = sum(repo["stargazerCount"] for repo in repositories["nodes"])

    while repositories["pageInfo"]["hasNextPage"]:
        cursor = repositories["pageInfo"]["endCursor"]
        if not cursor:
            raise RuntimeError("GitHub omitted the cursor while more repositories remained")
        page = graphql(token, REPOSITORY_PAGE_QUERY, {"login": username, "cursor": cursor})[
            "repositories"
        ]
        stars += sum(repo["stargazerCount"] for repo in page["nodes"])
        repositories = page

    totals = {"commits": 0, "prs": 0, "issues": 0}
    years = profile["contributionsCollection"]["contributionYears"]
    for year in years:
        counts = graphql(
            token,
            YEAR_STATS_QUERY,
            {
                "login": username,
                "from": f"{year}-01-01T00:00:00Z",
                "to": f"{year}-12-31T23:59:59Z",
            },
        )["contributionsCollection"]
        totals["commits"] += counts["totalCommitContributions"]
        totals["prs"] += counts["totalPullRequestContributions"]
        totals["issues"] += counts["totalIssueContributions"]

    return {
        "stars": stars,
        "commits": totals["commits"],
        "prs": totals["prs"],
        "issues": totals["issues"],
        "repos": profile["repositoriesContributedTo"]["totalCount"],
    }


def local_name(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def recolor_radar(root):
    styles = [element for element in root if local_name(element.tag) == "style"]
    if len(styles) != 1:
        raise RuntimeError(
            f"Expected one SVG style block, found {len(styles)}; SVG left unchanged"
        )

    css = styles[0].text or ""
    radar_rules = list(re.finditer(r"\.radar\s*\{[^}]*\}", css, flags=re.DOTALL))
    if len(radar_rules) != 1:
        raise RuntimeError(
            f"Expected one radar color rule, found {len(radar_rules)}; SVG left unchanged"
        )

    css, replacements = re.subn(
        r"\.radar\s*\{[^}]*\}",
        ".radar {\nstroke-width: 2px;\nstroke: #10698C;\n"
        "fill: #02F1B6;\nfill-opacity: 0.22;\n}",
        css,
        count=1,
        flags=re.DOTALL,
    )
    if replacements != 1:
        raise RuntimeError("Could not update radar colors; SVG left unchanged")
    styles[0].text = css


def find_language_pie(root):
    width = float(root.get("width", "0").replace("px", ""))
    height = float(root.get("height", "0").replace("px", ""))
    candidates = []

    for index, element in enumerate(list(root)):
        if local_name(element.tag) != "g":
            continue
        match = re.fullmatch(
            r"\s*translate\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)\s*",
            element.get("transform", ""),
        )
        if not match:
            continue

        x, y = map(float, match.groups())
        text_count = sum(1 for child in element.iter() if local_name(child.tag) == "text")
        path_count = sum(1 for child in element.iter() if local_name(child.tag) == "path")
        if x < width * 0.15 and y > height * 0.45 and text_count >= 5 and path_count >= 3:
            candidates.append((index, element, x, y))

    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one language pie group in {SVG_PATH}, found {len(candidates)}; SVG left unchanged"
        )
    return candidates[0]


def find_footer(root):
    candidates = []
    for element in list(root):
        if local_name(element.tag) != "g" or element.get("transform"):
            continue
        text_count = sum(1 for child in element.iter() if local_name(child.tag) == "text")
        path_count = sum(1 for child in element.iter() if local_name(child.tag) == "path")
        if text_count >= 5 and path_count >= 2:
            candidates.append(element)

    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one existing counter footer in {SVG_PATH}, found {len(candidates)}; SVG left unchanged"
        )
    return candidates[0]


def make_stats_panel(x, y, stats):
    tag = lambda name: f"{{{SVG_NS}}}{name}"
    group = ET.Element(tag("g"), {"transform": f"translate({x:g}, {y:g})"})

    ET.SubElement(
        group,
        tag("rect"),
        {
            "x": "0",
            "y": "0",
            "width": "480",
            "height": "270",
            "rx": "8",
            "fill": "#0D1117",
            "stroke": "#254A65",
            "stroke-width": "2",
        },
    )

    def add_text(x_pos, y_pos, value, size, color, weight="400", anchor="start"):
        element = ET.SubElement(
            group,
            tag("text"),
            {
                "x": str(x_pos),
                "y": str(y_pos),
                "font-size": str(size),
                "font-weight": weight,
                "text-anchor": anchor,
                "fill": color,
            },
        )
        element.text = value

    add_text(24, 44, "Stats", 30, "#02F1B6", "500")

    rows = [
        ("☆", "Total Stars:", stats["stars"]),
        ("●", "Total Commits:", stats["commits"]),
        ("↗", "Total PRs:", stats["prs"]),
        ("ⓘ", "Total Issues:", stats["issues"]),
        ("▣", "Contributed to:", stats["repos"]),
    ]
    for row, (icon, label, value) in enumerate(rows):
        baseline = 88 + row * 40
        add_text(25, baseline, icon, 21, "#228DAF", "600")
        add_text(54, baseline, label, 18, "#E1ECEE")
        add_text(290, baseline, f"{value:,}", 18, "#E1ECEE")

    ET.SubElement(group, tag("circle"), {"cx": "415", "cy": "158", "r": "46", "fill": "#02F1B6"})
    ET.SubElement(
        group,
        tag("path"),
        {
            "d": (
                "M6.766 11.328c-2.063-.25-3.516-1.734-3.516-3.656 0-.781.281-1.625.75-2.188 "
                "-.203-.515-.172-1.609.063-2.062.625-.078 1.468.25 1.968.703.594-.187 "
                "1.219-.281 1.985-.281.765 0 1.39.094 1.953.265.484-.437 1.344-.765 "
                "1.969-.687.218.422.25 1.515.046 2.047.5.593.766 1.39.766 2.203 "
                "0 1.922-1.453 3.375-3.547 3.64.531.344.89 1.094.89 1.954v1.625 "
                "c0 .468.391.734.86.547C13.781 14.359 16 11.53 16 8.03 16 3.61 "
                "12.406 0 7.984 0 3.563 0 0 3.61 0 8.031a7.88 7.88 0 0 0 5.172 "
                "7.422c.422.156.828-.125.828-.547v-1.25c-.219.094-.5.156-.75.156 "
                "-1.031 0-1.64-.562-2.078-1.609-.172-.422-.36-.672-.719-.719 "
                "-.187-.015-.25-.093-.25-.187 0-.188.313-.328.625-.328.453 0 .844.281 "
                "1.25.86.313.452.64.655 1.031.655s.641-.14 1-.5c.266-.265.47-.5.657-.656"
            ),
            "transform": "translate(376, 119) scale(4.9)",
            "fill": "#10698C",
        },
    )
    return group


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    username = os.environ.get("USERNAME", "").strip()
    if not token or not username:
        raise RuntimeError("GITHUB_TOKEN and USERNAME must be provided by the workflow")
    if not SVG_PATH.is_file():
        raise RuntimeError(f"Generated SVG not found: {SVG_PATH}")

    stats = fetch_stats(token, username)

    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    tree = ET.parse(SVG_PATH)
    root = tree.getroot()
    if local_name(root.tag) != "svg" or root.tag != f"{{{SVG_NS}}}svg":
        raise RuntimeError("Generated file is not an SVG document")

    recolor_radar(root)
    index, pie, x, y = find_language_pie(root)
    footer = find_footer(root)
    root.remove(pie)
    root.remove(footer)
    root.insert(index, make_stats_panel(max(20, x - 20), y + 40, stats))
    output = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    SVG_PATH.write_bytes(output)
    print("Replaced the language pie with live GitHub stats in profile-night-rainbow.svg")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Could not create the GitHub stats panel: {error}", file=sys.stderr)
        sys.exit(1)
