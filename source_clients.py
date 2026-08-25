"""Application-lifetime ownership for asynchronous source clients and immutable descriptors."""
import os

import ard_client
import bq
import driver
import grants
from accessor import okf_fetch


class AsyncSourceClients:
    def __init__(self, http_client=None, grant_pool=None, sources_root=None):
        self.http = http_client
        self.grants = grant_pool
        self.sources_root = sources_root
        self.sec = None
        self.bigquery = None
        self.descriptor_count = 0
        self._owns_http = http_client is None
        self._owns_grants = grant_pool is None

    async def start(self):
        try:
            # All filesystem work happens before readiness. Requests see cached immutable frontmatter.
            self.descriptor_count = okf_fetch.preload_descriptors(self.sources_root)
            self.http = self.http or ard_client.create_async_http_client()
            self.sec = driver.AsyncSecClient(self.http)
            project = os.getenv("GOOGLE_CLOUD_PROJECT")
            credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            if project and credentials:
                self.bigquery = bq.AsyncBigQueryClient(
                    project, self.http, credentials_file=credentials)
            if self.grants is None and (os.getenv("GRANTS_URL") or os.getenv("DATABASE_URL")):
                # The pool connects on the first grants query. Grants are optional and must not
                # make Census, SEC, BigQuery, or the UI unavailable during a database outage.
                self.grants = grants.AsyncGrantPool()
            return self
        except BaseException:
            await self.close()
            raise

    def bind(self, context):
        context.http_client = self.http
        context.sec_client = self.sec
        context.bigquery_client = self.bigquery
        context.grant_pool = self.grants
        return context

    async def close(self):
        if self.grants is not None and self._owns_grants and hasattr(self.grants, "close"):
            await self.grants.close()
        if self.http is not None and self._owns_http:
            await self.http.aclose()
