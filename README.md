# EPG Claro para TiviMate

Gera todo dia (05:00, horário de Brasília) um EPG em formato XMLTV com a grade da
[Claro TV](https://www.claro.com.br/tv-por-assinatura/programacao/grade), 7 dias à frente.

**Link para colar no TiviMate (Configurações > EPG > Adicionar fonte > URL):**

https://brunosza.github.io/epg-claro/epg_claro.xml.gz

## Como funciona

- `claro_epg.py` lê a grade pela API pública da Claro e gera o arquivo XMLTV.
- `.github/workflows/epg.yml` roda o script todo dia no GitHub Actions e publica o arquivo no GitHub Pages.
- Se os segredos `XC_SERVIDOR`, `XC_USUARIO` e `XC_SENHA` estiverem cadastrados
  (Settings > Secrets and variables > Actions), o EPG é casado com os canais da lista
  Xtream Codes, ligando todas as versões de um canal (FHD1, FHD2, SD...) à mesma grade.

Para rodar na hora: aba **Actions** > **Atualizar EPG** > **Run workflow**.
