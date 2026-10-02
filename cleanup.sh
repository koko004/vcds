#!/bin/bash
# VCDS Docker Cleanup Script
# Limpia imágenes antiguas, contenedores parados y recursos no utilizados

echo "=== VCDS Docker Cleanup ==="
echo ""

# Parar y eliminar contenedor si existe
echo "1. Parando contenedor vcds..."
docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true

# Eliminar imágenes dangling
echo "2. Eliminando imágenes dangling..."
docker image prune -f

# Eliminar imágenes antiguas de vcds (mantener solo latest y la versión actual)
echo "3. Eliminando imágenes antiguas de koko004/vcds..."
docker images koko004/vcds --format '{{.Tag}}' | grep -v -E '^(latest|1\.0\.27)$' | while read tag; do
  echo "   Eliminando koko004/vcds:$tag"
  docker rmi "koko004/vcds:$tag" 2>/dev/null || true
done

# Limpiar contenedores parados
echo "4. Eliminando contenedores parados..."
docker container prune -f

# Limpiar volúmenes huérfanos
echo "5. Eliminando volúmenes huérfanos..."
docker volume prune -f

# Limpiar red no utilizada
echo "6. Eliminando redes no utilizadas..."
docker network prune -f

# Limpiar caché de build
echo "7. Limpiando caché de build..."
docker builder prune -f 2>/dev/null || true

# Mostrar espacio liberado
echo ""
echo "=== Resumen de espacio ==="
docker system df

echo ""
echo "=== Limpieza completada ==="
echo "Para levantar la aplicación: docker compose up -d"
