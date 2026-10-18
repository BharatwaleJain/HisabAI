# HisabAI - Local Business Assistant

HisabAI is an open-source, local-first assistant for Indian freelancers and small businesses. It helps create GST-aware invoices, manage client records, extract expense details from receipts and export expenses to CSV.

*This is a submission for the [Hacktoberfest Open-Source AI Challenge Week 2: Side Hustle](https://dev.to/challenges/hacktoberfest-week2-2026-10-12)*

## What I Built

HisabAI reduces repetitive paperwork for freelancers, consultants and small businesses. Users can create invoice PDFs, manage client details and extract expense information from receipts. It is designed for people who need a simple, affordable alternative to complex accounting tools and prefer to keep their financial documents on their own computers.

## Demo

![Dashboard](https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/s1shntex76qhkwobibml.png)

![Clients](https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/m0bthymw5njdk6beo6k7.png)

![Create Invoice](https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/78qudfnqz543r0spwxye.png)

![Generated Invoice](https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/a7bqwf9bgk4529uet4ss.png)

![Scan Receipt](https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/y0o12gxp495vipj5uk5z.png)

![Review Extraction](https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/xau1ri0zpkgf9adrckhy.png)

![Expenses](https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/gt7f30a8tupp7084u7za.png)

![Settings](https://dev-to-uploads.s3.us-east-2.amazonaws.com/uploads/articles/nk3hk3xjdm2sgsac1i2m.png)

## Code
Explore the source code, setup instructions and implementation:
https://github.com/BharatwaleJain/HisabAI

## How I Built It

Built with **Python, FastAPI, SQLite, Jinja2 and ReportLab**, HisabAI provides invoice generation, local record storage, receipt imports and CSV exports. For AI-powered receipt extraction, it uses the open-weight **Gemma 3 4B** model through **Ollama**. A rule-based fallback is available when Ollama is disabled or unavailable. AI inference runs locally, without a remote AI provider configured.

## Why Does Open Innovation Matter?

Open innovation makes AI more accessible to small businesses. Open-weight models and local inference allow users to experiment with AI without depending on paid hosted-model APIs or sending financial documents to remote AI services. Because the project is open source, developers can inspect, adapt and extend it with new models, document formats and features.

## Prize Categories
**Gemma — Best Use of Gemma:** HisabAI uses Google's open-weight Gemma 3 4B model through Ollama for local AI-assisted receipt and invoice data extraction. This helps freelancers and small businesses extract expense details while keeping AI inference on their own machine, without relying on a hosted AI API.

## Features

- Create and download GST-aware invoice PDFs
- Manage business, client, invoice and expense records
- Import receipts and extract expense details with local AI
- Export expenses to CSV
- Store data locally using SQLite
- Use basic workflows without Ollama

## Requirements

- Python 3.10+
- Ollama for Local AI Extraction
- Tesseract OCR for Image Text Extraction

## Run Locally

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
ollama pull gemma3:4b
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**.

To run without Ollama, set `HISABAI_DISABLE_OLLAMA=1` before starting the app.

## Privacy and Limitations

- Records and uploaded files are stored locally by default
- After setup, normal workflows can run offline
- The app does not encrypt stored data
- GST filing, GSTIN verification, IRN generation and automatic tax compliance are not supported
- Scanned PDF OCR and automated tests are not included yet
- Review all AI-extracted fields before saving or using them