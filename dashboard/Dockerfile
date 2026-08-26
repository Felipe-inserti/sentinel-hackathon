# Sentinel Dashboard -- build multi-stage, imagem final mínima via
# `output: "standalone"` do Next.js (ver next.config.ts). Sem credencial
# nenhuma embutida na imagem: autenticação com o GCP é via ADC, resolvida
# em runtime pela Service Account anexada ao serviço Cloud Run.

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* precisa existir em BUILD time (fica embutido no bundle do
# cliente) -- passado via --build-arg no comando de deploy (ver README).
ARG NEXT_PUBLIC_GOOGLE_CLIENT_ID
ARG NEXT_PUBLIC_GCP_PROJECT_ID
ENV NEXT_PUBLIC_GOOGLE_CLIENT_ID=${NEXT_PUBLIC_GOOGLE_CLIENT_ID}
ENV NEXT_PUBLIC_GCP_PROJECT_ID=${NEXT_PUBLIC_GCP_PROJECT_ID}
RUN npm run build

FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=8080
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 8080
CMD ["node", "server.js"]
