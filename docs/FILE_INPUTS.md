# 统一文件输入

Web 的选择、粘贴与拖拽，以及 QQ 单聊的 attachments，都交给
`attachments/manager.py::FileManager.upload_file_path`。单轮最多 10 个、单文件最多 64 MiB。
前端不再用图片/音视频白名单阻止上传；服务端负责后缀分类和内容校验。

## 保存位置

新文件保存在 `data/files/YYYY-MM-DD/<local_id>_<安全文件名>`，日期按服务端本地时间，
使用文件首次保存时的 created_time。所有 FileManager 保存入口共用此规则：Web、QQ、
原图片接口、自动获取的视频。原始文件名仅供展示，保存名去除路径与控制字符、标记括号，
限制 UTF-8 字节长度；唯一 ID 防止同名文件覆盖。

已有文件不搬迁，DB 中原有 path/local_id 继续有效。相同内容去重时复用已有路径，
包括旧目录和较早日期的路径，不每日复制同一文件。远端媒体对象命名规则不变。

## 模型上下文

文件信息只使用相对 TENTACLE_HOME 的路径：

```text
[file:data/files/YYYY-MM-DD/唯一ID_说明.md]
```

不添加文件名、大小、MIME 或附件说明标题；格式与 Agent 输出文件相同。
上传响应及历史 images 字段仍包含 UI 展示所需的结构化元数据，不额外拼入模型正文。
为兼容客户端，REST `files`、旧 `images` 和会话方法参数 images 保持有效，归档沿用 images 列。

- JPEG/PNG/GIF/WebP 经 magic bytes 验证后走现有图片通道。
- MP4/MOV/WebM、MP3/WAV/M4A/OGG/FLAC/AAC/AIFF 经 ffprobe 校验后走对应音视频通道，
  继续受现有时长和当前模型能力限制。
- TXT/MD/CSV/JSON/YAML/日志及常用代码文件自动展开小文本；UTF-8/BOM、UTF-16 BOM
  或 GB18030 解码，二进制不当作文本展开。
- PDF/DOCX/XLSX、其他类型本地保存，先只给路径，按需通过 file_read 或其他工具读取。
  未支持的媒体格式作为普通文件保留，不冒充已输入到模型的媒体。

自动展开要求单文件不超过 64 KiB、解码后不超过 32,000 字符，整轮不超过 64,000 字符。
超出时只提供完整文件路径，不静默截掉一部分正文。无需远端 Files/GCS 配置也能收普通文件；
现有图片/音视频仍依赖相应上传配置。

小文本的**完整展开正文**在标记后作为普通 user 文本块进入 session.history，随每次模型
请求回传，并由现有 session cache 持久化。后续回合、重启恢复不丢弃它，也不因原文件修改
重新读取替换这份历史快照；会话压缩/切换仍遵循已有统一规则。
可见归档正文只保留用户文字和路径标记；UI 用已有结构化引用渲染文件卡片，不重复展示标记。

## 读取工具

主 Agent `file_read(path, offset=0, limit=20000, frame=0)`，与子 Agent 的同名只读 broker
是独立命名空间。主工具接受相对路径，复用出站文件的路径保护。
文本返回 content 与 next_offset，offset 按提取后的字符计数，next_offset 非空时可继续读取。
图片返回内部图像块，由模型方言转换为对应的图片输入；模型必须开启视觉能力。
预览统一经 FileManager 保存，读取图片无需配置远端 Files API，远端引用按当前模型按需解析。

支持以下格式：

| 类型 | 读取内容 |
| --- | --- |
| 文本与代码 | 常用代码、配置、JSON/JSONL、CSV/TSV、字幕、日历、补丁等；HTML/SVG 返回源码；无后缀及未知后缀也尝试文本识别，拒绝二进制控制字符 |
| 图片 | JPEG/PNG/GIF/WebP/BMP/TIFF/ICO/AVIF、PPM/PGM/PBM、TGA/PCX/PSD/JPEG2000；按内容解码为 PNG 预览，不执行外部渲染器 |
| PDF | 按页提取文字 |
| Word | DOCX/DOCM/DOTX/DOTM 段落（包括表格内段落） |
| Excel | XLSX/XLSM/XLTX/XLTM 工作表 TSV，使用缓存值，不计算公式 |
| PowerPoint | PPTX/PPTM/POTX/POTM/PPSX/PPSM 按幻灯片顺序提取正文与表格文字 |
| OpenDocument | ODT/OTT、ODS/OTS、ODP/OTP，以及 FODT/FODS/FODP；表格返回 TSV |
| RTF | 正文、Unicode 和代码页转义，跳过嵌入对象 |
| EPUB | 按书籍 spine 顺序提取可见正文 |
| EML/MHT/MHTML | 邮件头和一个首选正文，不展开邮件附件，不加载外部资源 |
| ZIP/TAR/TGZ/TBZ2/TXZ | 只列条目名称和大小，支持 TAR.GZ/TAR.BZ2/TAR.XZ |
| GZIP | 有界解压文本后读取，沿用字符分页 |

文本支持 UTF-8/BOM、UTF-16/UTF-32 BOM 和 GB18030。
图片限制 1600 万像素，预览长边最多 2048 像素，移除元数据；动图及多页 TIFF 默认第一帧/页，
用 `frame`（从 0 开始）选择其他帧/页，图片不能使用字符 offset。预览不代替原始文件。
不执行文件、宏、公式或外部链接；不自动解压用户 ZIP 到文件系统。
PDF 使用 pypdf，图片使用 [Pillow](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html)，
RTF 使用 [striprtf](https://github.com/joshy/striprtf)，Office 使用标准库 ZIP/XML；依赖版本见 requirements.txt。
扫描 PDF 仍需要 OCR；旧 DOC/XLS/PPT、7Z/RAR、HEIC/HEIF 需要先转换，SVG 只读源码。

解析在独立进程中串行运行，超时 20 秒；Linux 子进程地址空间上限 256 MiB、CPU 15 秒，
Office/EPUB 声明解压体积总和不超过 64 MiB，压缩包条目最多 10000 个。
Windows 使用文件句柄核对实际打开的路径，保留认证路径、特殊文件和硬链接保护。
限制失败向模型返回错误，不拖垮应用事件循环。
PDF 提取的能力与局限参见 [pypdf 文档](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。

## QQ 与 Web

QQ 接收纯附件消息，不再因 content 为空丢弃；先完成白名单校验，再有界下载官方 CDN
附件，逐次检查重定向目标，不携带 QQBot API 的鉴权头。单文件下载限时 60 秒，超限/失败
明确反馈，不把未收到的文件交给模型。文件先完整落盘，再进入共享 AgentSession。
支持的 CDN 域包括 qq.com、qq.com.cn、qpic.cn、qlogo.cn、gtimg.cn、myqcloud.com 的子域。

Web 普通文件在待发送区、用户气泡与历史中均作为文件展示，点击下载继续使用 REST 双层鉴权。
`/api/files/{local_id}/content` 对普通文件补做完整鉴权并强制 attachment/nosniff；
已有媒体渲染端点的设备校验保持原有规则。

鸿蒙沿用原上传/发送 API，可逐步放开文件选择并按 mime 渲染普通文件卡片。
自动文本准备在共享后端执行，客户端无需自行展开文本或发送本地绝对路径。

验证仅用临时库、合成文档和 fake QQ/CDN；不访问私人文件/对话或真实发送消息。
