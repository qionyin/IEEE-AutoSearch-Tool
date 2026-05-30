# Paper Daily

每天自动追踪 IEEE 新论文，按你的研究方向打分，并生成中文论文摘要。项目使用 GitHub Actions 自动抓取论文，用 GitHub Pages 展示网页。

## 你需要配置什么

| 配置 | 必须吗 | 说明 |
| --- | --- | --- |
| GitHub Pages | 必须 | 不开启就看不到网页 |
| 研究方向 | 建议配置 | 不配置会使用仓库自带示例方向 |
| 模型 API Key | 可选但推荐 | 不配置也能抓论文，但摘要会比较基础 |
| 其他运行参数 | 可不配置 | 默认值已经可以直接使用 |

## 第 1 步：Fork 或上传项目

把这个项目 Fork 到你的 GitHub 账号，或者上传到你自己的仓库。

下面假设你的仓库地址是：

```text
https://github.com/你的用户名/你的仓库名
```

## 第 2 步：开启 GitHub Pages

进入你的仓库页面，依次打开：

```text
Settings -> Pages -> Build and deployment -> Source
```

把 `Source` 选择为：

```text
GitHub Actions
```

保存后，网页会由 Actions 自动发布。

运行成功后，你可以在这里看到访问链接：

```text
Settings -> Pages
```

链接通常长这样：

```text
https://你的用户名.github.io/你的仓库名/
```

例如这个仓库对应的形式是：

```text
https://Futuresxy.github.io/paper-daily/
```

## 第 3 步：配置研究方向

推荐用 Issue 配置，后续修改最方便。

1. 打开仓库的 `Issues`。
2. 点击 `New issue`。
3. 选择 `Research Interests` 模板。
4. 修改 JSON 里的 `name`、`description`、`keywords`、`IEEE_categories`。
5. Issue 标题保持为 `Research Interests`。
6. 点击提交。

一个方向大概长这样：

```json
{
  "id": "llm_quantization",
  "name": "大模型低精度量化",
  "description": "关注 LLM 量化、低比特推理、KV cache 量化和推理性能优化。",
  "keywords": [
    "LLM quantization",
    "low-bit quantization",
    "INT4",
    "FP8",
    "KV cache quantization"
  ],
  "IEEE_categories": ["cs.CL", "cs.LG", "cs.AI"]
}
```

新手建议：

- `keywords` 尽量写英文，因为 IEEE 论文标题和摘要主要是英文。
- 每个方向先写 5 到 10 个关键词即可。
- 不确定分类时，可以先用 `cs.CL`、`cs.LG`、`cs.AI`。

## 第 4 步：配置模型 API Key（可选）

不配置 API Key 也能运行；配置后中文摘要质量会更好。

进入：

```text
Settings -> Secrets and variables -> Actions -> Secrets -> New repository secret
```

如果你用 DeepSeek，添加：

| Name | Secret |
| --- | --- |
| `DEEPSEEK_API_KEY` | 你的 DeepSeek API Key |

如果你用 OpenAI，添加：

| Name | Secret |
| --- | --- |
| `OPENAI_API_KEY` | 你的 OpenAI API Key |

如果你用其他 OpenAI-compatible 服务，添加：

| Name | Secret |
| --- | --- |
| `LLM_API_KEY` | 你的服务商 API Key |

如果你需要指定模型或服务地址，再到：

```text
Settings -> Secrets and variables -> Actions -> Variables -> New repository variable
```

可选添加：

| Name | 示例 |
| --- | --- |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` |
| `LLM_MODEL` | `deepseek-chat` |

只用 DeepSeek 或 OpenAI 的默认地址时，可以不填这两个变量。

## 第 5 步：第一次手动运行

进入：

```text
Actions -> Paper Daily -> Run workflow
```

第一次建议保持默认：

```text
lookback_days = 7
```

这表示第一次先拉取最近 7 天的相关论文。

点击绿色的 `Run workflow` 后等待运行完成。成功后，打开你的 GitHub Pages 链接即可查看网页：

```text
https://你的用户名.github.io/你的仓库名/
```

## 之后会自动更新

项目默认每天北京时间 06:00 自动运行一次。

第一次手动运行会初始化最近几天的论文；之后每天定时运行会进入增量模式，只拉取上次成功运行后新增的论文，不会每天把所有历史相关论文重新拉一遍。

网页里可以查看：

- 当天拉取的新论文
- 本周论文
- 本月论文
- 本周最相关的精选论文
- 按日期回看本周每天拉取的新论文
- 直接点击 `下载 PDF` 保存论文

## 本地预览

如果你想在自己电脑上预览页面：

```bash
python -m http.server 8000 --directory web
```

浏览器打开：

```text
http://localhost:8000
```
