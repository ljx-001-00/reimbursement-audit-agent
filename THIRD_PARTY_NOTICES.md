# 第三方组件与资源说明

源码中的许可文件位于 `assets/FONT-LICENSE.txt`、`assets/licenses` 及相应依赖包目录；便携版中的资源位于 `_internal/assets` 及 `_internal` 中相应组件目录。重新分发时请保留完整许可文件和声明。

便携版包含 CPython 3.10 及 Tcl/Tk 运行库。完整许可见 `assets/licenses/Python-LICENSE.txt`（包含 PSF、Tcl/Tk 及微软分发附加条件）和 `assets/licenses/Tk-license.terms`；便携版对应位置为 `_internal/assets/licenses/`。

便携版在 `_internal/onnxruntime/capi/` 中附带 Microsoft Visual C++ 可再发行运行库（msvcp140.dll、MSVCP140_1.dll、vcruntime140.dll、vcruntime140_1.dll）与 Universal C Runtime（ucrtbase.dll），版本为 14.51.36247.0 / 10.0.26100.4654，源自 Microsoft 可再发行组件，按其许可随程序分发；源码中同一组文件位于 `assets/msvc/`。onnxruntime 扩展模块依赖这些运行时，缺少它们时 OCR 无法加载。

中文PDF字体：Reimbursement Sans SC，基于Google Fonts Noto Sans SC修改并重命名；SIL Open Font License 1.1，完整许可见assets/FONT-LICENSE.txt。

OCR模型随RapidOCR官方发行包提供，PP-OCRv6 small检测/识别与移动方向模型，相关组件许可见下列分发元数据。

在线模型：可自行配置支持工具调用的 OpenAI 兼容 Chat Completions 服务、模型名称及 API 密钥，不再限定 GLM 或单个厂商。默认示例为 GLM-4.7-Flash；模型服务和密钥不随程序提供。服务费用、限额和权限以用户选择的平台为准。未使用真实服务密钥完成联调的接口，不作已验证声明。本地 OCR、规则审核和 PDF 导出无需模型服务。

## rapidocr 3.10.0

许可：Apache-2.0
项目：Documentation, https://rapidai.github.io/RapidOCRDocs

## onnxruntime 1.23.2

许可：MIT License
项目：https://onnxruntime.ai

## numpy 2.2.6

许可：BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0
项目：homepage, https://numpy.org

## shapely 2.1.2

许可：BSD-3-Clause
项目：https://github.com/shapely/shapely

## opencv-python-headless 4.11.0.86

许可：Apache 2.0
项目：https://github.com/opencv/opencv-python

## pypdfium2 5.14.0

许可：BSD-3-Clause, Apache-2.0, dependency licenses
项目：https://github.com/pypdfium2-team/pypdfium2

## reportlab 5.0.1

许可：BSD license (see license.txt for details), Copyright (c) 2000-2025, ReportLab Inc.
项目：https://www.reportlab.com/

## pillow 12.3.0

许可：MIT-CMU
项目：Release notes, https://pillow.readthedocs.io/en/stable/releasenotes/index.html

## pyinstaller 6.22.3

许可：GPLv2-or-later with a special exception which allows to use PyInstaller to build and distribute non-free programs (including commercial ones)
项目：Homepage, https://pyinstaller.org
