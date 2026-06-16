import os
import sys
import argparse
import requests
from app.diff_parser import parse_diff
from app.prompt_builder import build_review_prompt
from app.llm_client import call_llm
from app.output_parser import parse_and_sort_comments

def post_github_review(owner : str, repo : str, pr_number : str, token : str, commit_id : str, comments : list):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
        "Content-Type": "application/json"
    }

    payload = {
        "commit_id": commit_id,
        "event": "COMMENT",
        "body": "### Automated Code Review Feedback\nReview completed successfully.",
        "comments": comments
    }

    response = requests.post(url, json=payload, headers=headers)

    if response.status_code not in [200, 201]:
        print(f"Failed to post review: Status: {response.status_code}")
        print(f"Response: {response.text}")
        sys.exit(1)

    print("Code review posted successfully to GitHub.")

    def main():
        parser = argparse.ArgumentParser(description="Code Review Agent")
        parser.add_argument("--diff_file", help="Path to raw git diff file")
        args = parser.parse_args()

        if args.diff_file:
            with open(args.diff_file, "r") as f:
                diff_text = f.read()

        else:
            diff_text = sys.stdin.read()

        if not diff_text.strip():
            print("Empty diff. Exiting gracefully without process actions.")
            return

        parsed_diff = parse_diff(diff_text)
        prompt = build_review_prompt(parsed_diff)

        print("Instantiating LLM for code review...")

        try:
            raw_response = call_llm(prompt)
        except Exception as e:
            print(f"Critical System Error: Remote model client execution collapsed. Error: {e}")
            sys.exit(1)

        sorted_comments = parse_and_sort_comments(raw_response)

        if not sorted_comments:
            print("No critical findings reported by reviewer model.")
            return

        print("\n---Model Findings (Priority Order)---")
        for c in sorted_comments:
            print(f"[{c.severity}] {c.file}:{c.line} -> {c.comment}")
        print("-----------------------------------\n")

        gh_comments = []
        for c in sorted_comments:
            if c.file in parsed_diff:
                line_map = parsed_diff[c.file]['line_to_position']
                if c.line in line_map:
                    diff_position = line_map[c.line]
                    gh_comments.append({
                        "path": c.file,
                        "position": diff_position,
                        "body": f"###[{c.severity}] Priority Finding\n{c.comment}"
                    })
                else:
                    print(f"Warning: Line {c.line} not found in diff boundaries of {c.file}. Skipping.")
            else:
                print(f"Warning: File {c.file} not recognized in active changes. Skipping.")    

