# SO Task Intelligence Agent

AI assistant cho team **Sales Operations (SO)**. Service đọc `CLAUDE.md` làm
system prompt, nhận dữ liệu task qua HTTP, gọi LLM (OpenAI-compatible) và trả về
báo cáo ngày / tuần bằng tiếng Việt.

## Kiến trúc

```
Người dùng ──HTTP POST /chat──> FastAPI (agent.py) ──> LLM (OpenAI-compatible)
                                      │
                                      └── system prompt = CLAUDE.md
```

- **`agent.py`** — FastAPI app, endpoint `/chat` và `/health`.
- **`CLAUDE.md`** — toàn bộ logic / system prompt của agent.
- **`requirements.txt`** — dependencies.
- **`Dockerfile`** — đóng gói để deploy.
- **`.env.example`** — mẫu cấu hình.

## Cấu hình

Copy `.env.example` thành `.env` và điền API key:

```bash
cp .env.example .env
```

| Biến             | Ý nghĩa                                              | Mặc định                                         |
|------------------|------------------------------------------------------|--------------------------------------------------|
| `LLM_API_KEY`    | API key GreenNode MaaS (lấy từ email BTC)            | —                                                |
| `OPENAI_BASE_URL`| Endpoint GreenNode MaaS (phải có `/v1`)             | `https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1`|
| `MODEL_NAME`     | Tên model                                            | `minimax/minimax-m2.5`                           |
| `TEMPERATURE`    | Độ sáng tạo                                          | `0.3`                                            |
| `MAX_TOKENS`     | Giới hạn token output                                | `2048`                                           |
| `PORT`           | Cổng service                                         | `8000`                                           |

> Service dùng giao thức **OpenAI-compatible** nên trỏ thẳng vào GreenNode MaaS.
> Vẫn tương thích ngược với OpenAI: nếu đặt `OPENAI_API_KEY` / `MODEL` thì code
> tự dùng làm fallback.

## Chạy local (Python)

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
uvicorn agent:app --reload --port 8000
```

## Chạy bằng Docker

```bash
docker build -t so-agent .
docker run --rm -p 8000:8000 --env-file .env so-agent
```

## API

### `GET /health`

```json
{ "status": "ok" }
```

### `POST /chat`

Request:

```json
{
  "message": "báo cáo hôm nay",
  "history": [
    { "role": "user", "content": "..." },
    { "role": "assistant", "content": "..." }
  ]
}
```

- `message` (bắt buộc): tin nhắn / dữ liệu task dán vào.
- `history` (tùy chọn): các lượt hội thoại trước để giữ ngữ cảnh.
- `model`, `temperature` (tùy chọn): override mặc định.

Response:

```json
{
  "reply": "📋 BÁO CÁO NGÀY ...",
  "model": "gpt-4o-mini",
  "usage": { "prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801 }
}
```

### Ví dụ `curl`

```bash
# Health
curl http://localhost:8000/health

# Chat
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "báo cáo tuần này"}'
```

Tài liệu tương tác (Swagger UI): http://localhost:8000/docs

## Cách hoạt động

1. Khi khởi động, app đọc `CLAUDE.md` và cache làm system prompt.
2. Mỗi request `/chat` ghép: `system prompt + history + message` rồi gọi LLM.
3. Trả về `reply` cùng thông tin model và token usage.

Muốn đổi hành vi agent (format báo cáo, tone, quy tắc...) → chỉ cần sửa
`CLAUDE.md`, không cần đụng vào code.
