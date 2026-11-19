# Self-Hosted AI Research & Automation on NVIDIA DGX Spark

Six agents on my DGX Spark that research AI and PM news, verify claims, and deliver briefs via Telegram.

AI personal research system built on NVIDIA DGX Spark using Hermes mainly, with Telegram as the interface. I also experimented with OpenClaw.

I built this system to keep up with AI without checking every source individually:  **AI development moves fast across X, YouTube, research papers, frontier lab posts, and independent blogs.** This allowed me to stop spending hours looking for this information and dig into whatever interested me. 

The system separates the collection, research, synthesis, and delivery into different workflows. It supports scheduled briefs, selective alerts, cross-agent trend synthesis.

I also built an additional agent to help with keeping up with the latest product management trends and news. This agent aggregates information such as book recommendations, new concepts, and trends across product management. I also built a job-search agent that recommends roles based on the specific things I am looking for. 


## Why I built it

I know it is a struggle to keep up with AI news. Even people in the field have to be vigilant about looking through multiple forms of media to figure out what is going on. I wanted a system that helped me understand:

- What is going on?
- What actually changed?
- Why does it matter?
- Have I already seen this story?
- Does it warrant an interruption, a daily summary, or deeper reading?
  

## Agents

| Agent | Responsibility |
|---|---|
| **AI News** | Collects news, model releases, benchmark updates into daily Telegram digest and full report with separate major news alert and Todoist reading tasks. |
| **X Research** | Looks into significant AI discussions on X, checks original claims against supporting evidence, and delivers briefs. |
| **YouTube Research** | Monitors 500 channels, selects 10 recent videos for transcript based review, and delivers timestamped summaries with evidence checks, caveats, and takeaways. |
| **AI Trends Dashboard** | Reads the other research bots' completed work, merges overlapping stories, and updates a persistent dashboard when meaningful trends or evidence changes. |
| **Job Search** | Finds senior product roles, verifies openings, and evaluates fit against a privately supplied resume. |
| **Product Management Reports** | Finds PM practices, foundations, and books throughout the week, then produces weekly learning edition with examples and sources. |

## Architecture

```mermaid
flowchart LR
    News["News, papers and lab updates"] --> AI["AI News"]
    AI --> Daily["Daily digest + full report"]
    AI --> Alerts["Major-news alerts"]

    X["X"] --> XR["X Research"]
    YouTube["YouTube"] --> YR["YouTube Research"]

    AI --> Trends["AI Trends Dashboard"]
    XR --> Trends
    YR --> Trends

    Jobs["Job listings + resume"] --> JobAgent["Job Search"]
    PM["PM research + books"] --> PMAgent["PM Learning Reports"]

    Daily --> Telegram["Telegram"]
    Alerts --> Telegram
    XR --> Telegram
    YR --> Telegram
    Trends --> Telegram
    JobAgent --> Telegram
    PMAgent --> Telegram
```

Agents run independently through Hermes on the DGX. The dashboard synthesizes their completed AI research. AI News and X also maintain Todoist reading lists.


**Hosting:** NVIDIA DGX Spark  
**Agent framework:** Hermes  
**Interface:** Telegram  
**Inference:** Local models include MiniMax-M2.7 (UD-IQ4_XS via llama.cpp), Qwen3 30B-A3B, and Qwen3.5 27B (Q4_K_M and Q8_0 via Ollama). The exported primary agent configurations currently use cloud Codex.
**Automation:** Hermes scheduled jobs and Linux services

Self-hosting the agents does not mean every inference request stays on-device. Provider choice determines where model processing occurs.


## What I built

I used Hermes to build and run these agents on my DGX Spark, I chose what each agent should cover, which sources to use, how to check the information, and when to send me updates through Telegram. 

I used AI-assisted coding to develop and adapt the scripts for collecting information, reviewing transcripts, checking outputs, and delivering reports. I also configured the models and schedules and worked through issues like repeated news, missed updates, and model usage limits.


## Repository structure

```text
agents/
├── ai-news/
├── x-research/
├── youtube-research/
├── ai-trends-dashboard/
├── job-search/
├── orchestrator/
└── product-management-reports/

EXPORT-NOTES.txt
.gitignore
```

Each agent folder contains its instructions, configuration examples, schedule templates, and available implementation files. Custom scripts, tests, and deployment examples are included where applicable.

The product-management folder also contains exported report-building code and templates.

## Current status

These files come from my own DGX setup. I removed credentials and personal data before sharing them.

To use them, you’ll need to configure your own paths, model access, Telegram recipients, and dependencies. Schedule examples are disabled by default. I haven’t tested the exported setup as a fresh installation.

See [EXPORT-NOTES.txt](EXPORT-NOTES.txt) for setup details and known gaps.
