// Run from /app in an isolated recovery container made from the pinned app image.
// Uses the CPU embedding service directly; never calls native chat/model controls.
import 'reflect-metadata';
import { Ignitor } from '@adonisjs/core';
import { QdrantClient } from '@qdrant/js-client-rest';
import { Ollama } from 'ollama';
import { writeFile } from 'node:fs/promises';

const target = new URL(process.env.QDRANT_RECOVERY_URL);
if (!target.hostname.startsWith('nomad-index-')) throw new Error('Select an isolated recovery index');
const file = process.env.RECOVERY_ZIM;
if (!file?.startsWith('/app/storage/zim/')) throw new Error('Select a mounted recovery ZIM');
const root = new URL('file:///app/');
const app = new Ignitor(root, {
  importer: path => import(path.startsWith('.') ? new URL(path, root).href : path),
}).tap(app => app.booting(() => import('#start/env'))).createApp('console');
await app.init();
await app.boot();
try {
  const { RagService } = await import('/app/app/services/rag_service.js');
  const embeddings = new Ollama({ host: 'http://nomad_embeddings:11434' });
  const models = await embeddings.list();
  if (!models.models.some(m => m.name === 'nomic-embed-text:v1.5')) throw new Error('CPU embedding model missing');
  const rag = new RagService({}, { getClient: async () => embeddings });
  rag.qdrant = new QdrantClient({ url: target.href });
  rag.embeddingModelVerified = true;
  rag.sourceActive = async () => process.env.RECOVERY_ACTIVE !== 'false';
  let offset = 0;
  let chunks = 0;
  for (;;) {
    const result = await rag.processZIMFile(file, false, offset, undefined,
      process.env.RECOVERY_COLLECTION || undefined);
    if (!result.success) throw new Error('ZIM rebuild failed');
    offset += result.articlesProcessed;
    chunks += result.chunks;
    console.log(JSON.stringify({ articles: offset, chunks, more: result.hasMoreBatches }));
    if (!result.hasMoreBatches) break;
    if (!result.articlesProcessed) throw new Error('ZIM rebuild stopped making progress');
  }
  await writeFile('/tmp/zim-rebuild.json', JSON.stringify({ articles: offset, chunks }) + '\n');
} finally {
  await app.terminate();
}
