#!/bin/bash
# Совместимость: старые закладки /cgi-bin/index.cgi → корень SPA
echo "Status: 302 Found"
echo "Location: /"
echo "Content-Type: text/plain; charset=UTF-8"
echo "Cache-Control: no-store"
echo ""
echo "See /"
