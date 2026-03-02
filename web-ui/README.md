# AI Research Assistant - Web UI

A modern, animated web interface for the AI-powered multi-source research tool.

## Features

- **Chat Interface**: Natural language query input with suggestion buttons
- **Animated Loading**: Visual feedback during research (planning → searching → gathering → synthesizing)
- **Tabbed Results**: 
  - **Summary**: AI-synthesized findings with key points
  - **Sources**: List of all checked sources with relevance scores
  - **Raw Data**: Full content from each source
- **Code Highlighting**: Syntax-highlighted code blocks in results
- **Markdown Tables**: Properly formatted tables from research results
- **Responsive Design**: Works on desktop and mobile

## Setup

### Prerequisites

- Node.js 18+ 
- Python backend running (FastAPI on port 8000)
- Ollama running for AI synthesis

### Installation

1. Navigate to the web-ui directory:
```bash
cd web-ui
```

2. Install dependencies:
```bash
npm install
```

3. Run the development server:
```bash
npm run dev
```

4. Open [http://localhost:3000](http://localhost:3000) in your browser

### Running the Full Stack

You need to run both the Python backend and the Next.js frontend:

**Terminal 1 - Python Backend:**
```bash
# From the project root
python -m web_scraper.api
```

**Terminal 2 - Next.js Frontend:**
```bash
cd web-ui
npm run dev
```

## Usage

1. Type your research query in the chat input
2. Toggle "Deep research" for more detailed content
3. Set "Max sources" to control how many sources to check (optional)
4. Press Enter or click the send button
5. Watch the animated loading states
6. View results in three tabs:
   - **Summary**: AI-synthesized answer with key findings
   - **Sources**: All checked sources with URLs and relevance
   - **Raw Data**: Full content from each source

## Architecture

- **Frontend**: Next.js 15 + React 19 + TypeScript
- **Styling**: Tailwind CSS v4
- **Animations**: Framer Motion
- **Markdown**: react-markdown + react-syntax-highlighter
- **Icons**: Lucide React
- **Backend**: FastAPI (Python) - runs separately on port 8000
