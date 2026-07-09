# feishu_doc.py
# -*- coding: utf-8 -*-
import logging
import json
import lark_oapi as lark
from lark_oapi.api.docx.v1 import *
from typing import List, Dict, Any, Optional
from src.config import get_config

logger = logging.getLogger(__name__)


class FeishuDocManager:
    """飞书云文档管理器 (基于官方 SDK lark-oapi)"""

    def __init__(self):
        self.config = get_config()
        self.app_id = self.config.feishu_app_id
        self.app_secret = self.config.feishu_app_secret
        self.folder_token = self.config.feishu_folder_token

        # 初始化 SDK 客户端
        # SDK 会自动处理 tenant_access_token 的获取和刷新，无需人工干预
        if self.is_configured():
            self.client = lark.Client.builder() \
                .app_id(self.app_id) \
                .app_secret(self.app_secret) \
                .log_level(lark.LogLevel.INFO) \
                .build()
        else:
            self.client = None

    def is_configured(self) -> bool:
        """检查配置是否完整"""
        return bool(self.app_id and self.app_secret and self.folder_token)

    def upload_file_to_folder(self, file_path: str, file_name: Optional[str] = None) -> Optional[str]:
        """
        上传本地文件到配置的飞书文件夹（drive upload_all），返回文件访问链接。
        SDK 不支持或上传失败时返回 None（fail-open，不影响文档创建主流程）。
        """
        if not self.client or not self.is_configured():
            return None
        try:
            import os

            from lark_oapi.api.drive.v1 import (
                UploadAllFileRequest,
                UploadAllFileRequestBody,
            )

            name = file_name or os.path.basename(file_path)
            size = os.path.getsize(file_path)
            with open(file_path, "rb") as f:
                request = UploadAllFileRequest.builder() \
                    .request_body(UploadAllFileRequestBody.builder()
                                  .file_name(name)
                                  .parent_type("explorer")
                                  .parent_node(self.folder_token)
                                  .size(size)
                                  .file(f)
                                  .build()) \
                    .build()
                response = self.client.drive.v1.file.upload_all(request)
            if not response.success():
                logger.warning(f"飞书文件上传失败: {response.code} - {response.msg}")
                return None
            file_token = getattr(response.data, "file_token", None)
            if not file_token:
                logger.warning("飞书文件上传成功但未返回 file_token")
                return None
            file_url = f"https://feishu.cn/file/{file_token}"
            logger.info(f"飞书文件上传成功: {name} ({file_url})")
            return file_url
        except Exception as e:
            logger.warning(f"飞书文件上传异常（已忽略）: {e}")
            return None

    def create_daily_doc(self, title: str, content_md: str) -> Optional[str]:
        """
        创建日报文档
        """
        if not self.client or not self.is_configured():
            logger.warning("飞书 SDK 未初始化或配置缺失，跳过创建")
            return None

        try:
            # 1. 创建文档
            # 使用官方 SDK 的 Builder 模式构造请求
            create_request = CreateDocumentRequest.builder() \
                .request_body(CreateDocumentRequestBody.builder()
                              .folder_token(self.folder_token)
                              .title(title)
                              .build()) \
                .build()

            response = self.client.docx.v1.document.create(create_request)

            if not response.success():
                logger.error(f"创建文档失败: {response.code} - {response.msg} - {response.error}")
                return None

            doc_id = response.data.document.document_id
            # 这里的 domain 只是为了生成链接，实际访问会重定向
            doc_url = f"https://feishu.cn/docx/{doc_id}"
            logger.info(f"飞书文档创建成功: {title} (ID: {doc_id})")

            # 2. 解析 Markdown 并写入内容
            # 将 Markdown 转换为 SDK 需要的 Block 对象列表
            blocks = self._markdown_to_sdk_blocks(content_md)

            # 飞书 API 限制每次写入 Block 数量（建议 50 个左右），分批写入
            batch_size = 50
            doc_block_id = doc_id  # 文档本身也是一个 block

            for i in range(0, len(blocks), batch_size):
                batch_blocks = blocks[i:i + batch_size]

                # 构造批量添加块的请求
                batch_add_request = CreateDocumentBlockChildrenRequest.builder() \
                    .document_id(doc_id) \
                    .block_id(doc_block_id) \
                    .request_body(CreateDocumentBlockChildrenRequestBody.builder()
                                  .children(batch_blocks)  # SDK 需要 Block 对象列表
                                  .index(-1)  # 追加到末尾
                                  .build()) \
                    .build()

                write_resp = self.client.docx.v1.document_block_children.create(batch_add_request)

                if not write_resp.success():
                    logger.error(f"写入文档内容失败(批次{i}): {write_resp.code} - {write_resp.msg}")

            logger.info(f"文档内容写入完成")
            return doc_url

        except Exception as e:
            logger.error(f"飞书文档操作异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _markdown_to_sdk_blocks(self, md_text: str) -> List[Block]:
        """
        将 Markdown 转换为飞书 SDK 的 Block 对象。

        结构解析（标题/加粗/表格/引用/列表）由 SDK 无关的
        src.feishu_md.parse_markdown_structure 完成，这里只做 SDK 映射。
        """
        from src.feishu_md import parse_markdown_structure

        kind_to_block_type = {"text": 2, "heading1": 3, "heading2": 4, "heading3": 5}
        blocks = []
        for kind, runs in parse_markdown_structure(md_text):
            if kind == "divider":
                blocks.append(Block.builder()
                              .block_type(22)
                              .divider(Divider.builder().build())
                              .build())
                continue

            elements = []
            for content, bold in runs:
                style_builder = TextElementStyle.builder()
                if bold:
                    style_builder.bold(True)
                text_run = TextRun.builder() \
                    .content(content) \
                    .text_element_style(style_builder.build()) \
                    .build()
                elements.append(TextElement.builder().text_run(text_run).build())

            text_obj = Text.builder() \
                .elements(elements) \
                .style(TextStyle.builder().build()) \
                .build()

            block_type = kind_to_block_type.get(kind, 2)
            block_builder = Block.builder().block_type(block_type)
            if block_type == 2:
                block_builder.text(text_obj)
            elif block_type == 3:
                block_builder.heading1(text_obj)
            elif block_type == 4:
                block_builder.heading2(text_obj)
            elif block_type == 5:
                block_builder.heading3(text_obj)
            blocks.append(block_builder.build())

        return blocks