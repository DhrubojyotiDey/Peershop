# PeerShop — Local Team

**Project:** PeerShop — Hyperlocal Multi-Vendor Marketplace
**Path:** /root/projects/peershop/
**Stack:** Python stdlib, SQLite, Telegram bot API, Gemini + Groq AI
**Status:** LIVE at shop.peershop.in (Cloudflare tunnel → CT 101:8000)
**Git:** /root/git/peershop.git (bare local)
**Deploy:** bash /root/projects/peershop/deploy.sh (dev→prod promotion)

## Environments
| | Production | Dev |
|---|---|---|
| Path | /root/projects/peershop | /root/projects/peershop-dev |
| Web | :8000 (Cloudflare tunnel) | :8100 (LAN only) |
| Admin | :8001 | :8101 |
| Public | shop.peershop.in | LAN only |

## Architecture
- Sellers list products via Telegram bot or web interface
- AI (Gemini + Groq) drafts listings and suggests taxonomy — human confirms
- Admin approves before anything goes live
- 9 departments: Electronics, Home & Kitchen, Apparels, Wellness, Grocery, Toys & Baby, Automotive & Industrial, Books & Media, Bags & Luggage
- 3-level taxonomy: department → category → subcategory

## Team Roster
| Role | File |
|---|---|
| Lead Engineer | /root/projects/peershop/team/lead_engineer.md |
| Developer | /root/projects/peershop/team/developer.md |
| UI/UX Designer | /root/projects/peershop/team/uiux_designer.md |
| DevOps | /root/projects/peershop/team/devops.md |
| Researcher | /root/projects/peershop/team/researcher.md |
| Tester | /root/projects/peershop/team/tester.md |

## Key Docs
- /root/projects/peershop/CLAUDE.md — full project overview
- /root/projects/peershop/DEVELOPMENT.md — dev/prod workflow
