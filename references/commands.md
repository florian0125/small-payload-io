# 命令与环境

脚本相对路径以本 skill 目录为基准。Windows 示例要求本地已安装 Python 3.10+ 且 python 命令可用；也可将 $python 设置为已有解释器的完整路径。示例输入、输出路径需按实际文件修改。文本脚本无第三方依赖；图片需要 Pillow，PDF 需要 pypdf。不要为了读取私有文件改用在线服务。

```powershell
$python = (Get-Command python -ErrorAction Stop).Source
$skillRoot = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $env:USERPROFILE '.codex\skills' }
$skill = Join-Path $skillRoot 'small-payload-io'
$io = Join-Path $skill 'scripts\safe_io.py'
$work = 'D:\task-output\small-payload'
$state = Join-Path $work 'budget.json'
# 同一任务不能换账本重置预算。
& $python -X utf8 $io init --state $state --task-id 'current-task-id'
& $python -X utf8 $io pack 'D:\input\long.txt' 'D:\input\notes.txt' --out "$work\packets"
& $python -X utf8 $io read "$work\packets\packet-000000.json" --state $state
& $python -X utf8 $io status --state $state
```

每条 read 后先分析并更新本地进度，再选择下一包，不能用 shell 循环把全部包输出到一次工具调用。index.json 是本地索引，可能很大；仅在本地解析所需记录，不全文打印。pack 保留 CRLF、Unicode，初始 UTF-8 BOM 不计入正文。字节偏移基于去除初始 BOM 后的 UTF-8。

图片压缩、局部裁切（坐标基于应用 EXIF 方向后的图像）与计费：

```powershell
& $python -X utf8 $io image 'D:\input\screen.png' --out "$work\overview.jpg"
& $python -X utf8 $io image 'D:\input\screen.png' --max-bytes 24576 --out "$work\smaller.jpg"
& $python -X utf8 $io image 'D:\input\screen.png' --crop 120 80 1120 780 --out "$work\detail.jpg"
# 仅在已获得图片发送授权时添加标记；标记本身不是授权。
& $python -X utf8 $io reserve-image "$work\detail.jpg" --state $state --user-approved
# 接着显示一次该 JPEG，禁止在终端输出 Base64。
```

若 functions 编排可用且 view_image 返回 data URL，先计算返回值实际编码大小，超限时不调用 image()。若工具重新编码导致放大，改用能保留原 JPEG 的受支持工具；无法保留编码时回退本地 OCR。reserve-image 不能拦截所有工具的实际上传。

`--max-bytes` 可降低单张目标（4096–65536），不能提高硬上限。太低会在质量下限处拒绝，而不是无限压缩成无法辨认的图。此机器实测 view_image 保留 JPEG 原始字节，但 MIME 可能为 application/octet-stream；确认 JPEG 字节和扩展名后，可在编排返回图像时使用 image/jpeg。禁止把 Base64 作为文本输出。

原生文档文字提取（--pages 为从 0 开始的页/幻灯片索引，DOCX 仅整体文档部件）：

```powershell
& $python -X utf8 "$skill\scripts\extract_text.py" 'D:\input\slides.pptx' --out "$work\slides.txt"
& $python -X utf8 "$skill\scripts\extract_text.py" 'D:\input\manual.pdf' --pages 0 1 --out "$work\pages.txt"
& $python -X utf8 $io pack "$work\slides.txt" --out "$work\slides-packets"
```

提取脚本限制单 XML 部件 32 MiB、累计 XML/文字 128 MiB；大型 PDF 先指定少量页，库解码内存仍取决于 PDF。本脚本不包含 OCR、表格结构恢复、备注、嵌入图片或公式语义重建。

长命令输出：

```powershell
& $python -X utf8 $io capture --out "$work\search.log" --timeout 120 -- rg -n --max-count 20 'target_symbol' 'D:\code'
& $python -X utf8 $io pack "$work\search.log" --out "$work\search-packets"
```

capture 不使用 shell=True，不自动解析管道；需要 PowerShell 语法时显式调用 PowerShell并正确引用参数。它限制等待时间和模型输出，不限制日志磁盘占用。根据退出状态判断命令是否成功。

交接：先写短 UTF-8 文件，记录目标、来源、已处理页/时间/包、结论与证据、下一步、外发限制。随后：

```powershell
& $python -X utf8 $io handoff --state $state --note "$work\progress.txt" --out "$work\handoff.json"
```

handoff 只写本地文件，不改旧账本、不创建新任务。新任务只读取精简说明和所需来源，不把完整旧任务、所有包、原始大图一并带入。
