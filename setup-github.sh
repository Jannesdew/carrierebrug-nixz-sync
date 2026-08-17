#!/bin/bash
set -e

cd "/Users/jannesdewilde/Downloads/2 - Wilde Webdesign/Projects/Carrièrebrug/API en automatisering/github-actions-nixz-sync"

# --- Bestanden op de juiste plek zetten ---
mv nixz-sync-gitignore.txt .gitignore
mkdir -p .github/workflows
mv nixz-sync.yml .github/workflows/nixz-sync.yml

# --- Git repo initialiseren ---
git init
git add .
git commit -m "Initial commit: NIXZ -> Airtable -> Webflow sync"

# --- Repo aanmaken op GitHub en pushen (vereist GitHub CLI 'gh') ---
gh repo create carrierebrug-nixz-sync --public --source=. --remote=origin --push

echo ""
echo "Repo staat live. Nu de secrets toevoegen:"
echo "  gh secret set NIXZ_USERNAME"
echo "  gh secret set NIXZ_PASSWORD"
echo "  gh secret set AIRTABLE_TOKEN"
echo "  gh secret set WEBFLOW_API_TOKEN"
