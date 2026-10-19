# Install dependencies with npm in a throwaway stage...
FROM node:22-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev

# ...then run on plain Alpine with its Node.js package (no npm, yarn or
# corepack), which is much smaller than the official node image.
FROM alpine:3.20
RUN apk add --no-cache nodejs && addgroup -S app && adduser -S app -G app
WORKDIR /app
ENV NODE_ENV=production
COPY --from=deps /app/node_modules ./node_modules
COPY package.json server.js ./
USER app
EXPOSE 5000
CMD ["node", "server.js"]
