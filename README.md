# Claude Usage Widget

*This is a plugin to Claude Code where you can see in your task bar 2 columns chart with the usage per session and per week.*

Widget de bandeja do Windows pra acompanhar o consumo da janela de 5h e da janela
semanal do Claude Code Pro, mais uma estimativa de custo em USD do mês corrente.

## Como funciona

- `statusline_writer.py` está registrado como `statusLine` em `~/.claude/settings.json`.
  Toda vez que o Claude Code renderiza a status line durante uma sessão ativa, ele
  passa `rate_limits.five_hour` e `rate_limits.seven_day` via stdin — esse script só
  grava isso em `~/.claude/usage-cache.json`. É o mecanismo oficial, sem chamada de
  rede nenhuma.
- `tray_widget.py` é o processo que fica na bandeja. Ele lê o cache local a cada
  ~20s (isso é só leitura de arquivo, sem rede). Quando o cache está velho, ele faz
  **uma única chamada de rede** a cada `NETWORK_POLL_INTERVAL_SECONDS` (hoje 2
  minutos) — nunca mais frequente que isso, tenha a chamada anterior funcionado ou
  não — pra `https://api.anthropic.com/api/oauth/usage`, usando o token OAuth que
  o próprio Claude Code já guarda em `~/.claude/.credentials.json`. Essa mesma
  chamada traz tanto o 5h/Semanal quanto os créditos de uso reais (CR$), então uma
  chamada só resolve os dois.
  (Motivo do cooldown existir: numa versão anterior, dois checadores independentes
  martelavam esse endpoint a cada ~20s sem nenhum cooldown, o que fez a Anthropic
  devolver 429/Too Many Requests. Testamos 2 minutos depois disso e não reproduziu
  o problema — mas se a Anthropic também começar a devolver 429 nesse intervalo, o
  widget respeita o `Retry-After` que ela manda e espera o tempo certo antes de
  tentar de novo.)
- Separadamente, ele varre os logs locais de sessão (`~/.claude/projects/**/*.jsonl`)
  pra somar tokens consumidos no mês corrente e estimar um custo em USD a preço
  público de API (ver `pricing.py`) — isso é o **CEC$** (Custo Estimado de Consumo).
  É incremental — só lê os bytes novos de cada arquivo a cada rodada, não reprocessa
  tudo do zero.
- **Renovação automática do token**: o `accessToken` do Claude Code expira a cada
  poucas horas. Se a chamada de uso vier com 401 (token expirado), o widget usa o
  `refreshToken` que já está em `~/.claude/.credentials.json` pra pedir um novo
  token (mesmo fluxo OAuth que o próprio Claude Code usa) e regrava o arquivo com
  os tokens novos antes de tentar de novo. Ele sempre faz um backup
  (`.credentials.json.bak`) antes de sobrescrever. Se o refresh também falhar (ex.
  o refresh token expirou de vez, o que leva bem mais tempo — semanas), aí sim só
  abrir o Claude Code resolve.

## Riscos que você precisa saber

- **O endpoint de fallback (`/api/oauth/usage`) não é documentado oficialmente pela
  Anthropic.** É o mesmo mecanismo que ferramentas da comunidade (ex.
  `claude-code-statusline`) já usam, mas pode mudar ou parar de funcionar sem aviso.
  Se isso acontecer, o widget mostra "—" no lugar do número — ele não trava.
- **CEC$ (Custo Estimado de Consumo) é uma estimativa**, calculada aplicando o preço
  público por token da API aos tokens que aparecem nos seus logs locais. **Não é a
  fatura real da sua assinatura Pro**, que é um valor fixo mensal. Serve só de
  referência pra você ver se está consumindo muito ou pouco em relação ao que
  pagaria na API avulsa.
- **CR$ (Custo Real) é o valor de verdade** — vem do campo `spend` da sua conta,
  o mesmo que aparece em Configurações → Consumo. Fica em 0 enquanto os créditos de
  uso estiverem desativados; se você ativar e passar a gastar além do Pro, reflete
  o valor real cobrado.
- **A renovação de token mexe no arquivo de login de verdade do Claude Code**
  (`~/.claude/.credentials.json`). O endpoint de refresh também não é documentado
  oficialmente, e o refresh token é de uso único (a cada renovação, ganha um
  refresh token novo) — por isso o widget sempre regrava o arquivo imediatamente
  após um refresh bem-sucedido, pra não deixar o Claude Code com um token velho e
  morto na mão. Se algo der errado no meio do caminho, tem um `.credentials.json.bak`
  do estado anterior.
- Os preços em `pricing.py` foram checados em julho/2026 — inclusive o desconto de
  lançamento do Sonnet 5, que vale só até 2026-08-31. Se a Anthropic mudar preços,
  atualiza esse arquivo (confira em anthropic.com/pricing).

## Rodar manualmente (modo dev)

```powershell
pip install -r requirements.txt
python tray_widget.py
```

Só uma instância roda por vez — se tentar abrir de novo, ele avisa e sai.

## Empacotar e deixar rodando sempre

```powershell
.\build.ps1
```

Isso gera `dist\claude-usage-widget.exe`. Cria um atalho dele na pasta Startup
(`Win+R` → `shell:startup`) pra ele subir sozinho com o Windows.

## Uso

- **Ícone na bandeja**: duas barrinhas verticais compactas (5h e semanal), cor vai
  de verde a vermelho conforme o consumo sobe. Passa o mouse pra ver o tooltip:
  `5h | S | CEC$ | CR$`.
- **Clique no ícone** (ou botão direito → Detalhes): abre um painel com as duas
  barras grandes, legendas "5h"/"S", e as linhas CEC$/CR$ com uma legenda pequena
  embaixo de cada uma. Clica em qualquer lugar do painel (ou clica fora) pra fechar.
- **Notificações**: um balão do Windows quando qualquer uma das duas janelas passa
  de 80% e de novo ao passar de 95%. Só avisa uma vez por faixa, reseta quando a
  janela em si reseta.

## Idioma

O widget detecta o idioma da interface do Windows (`GetUserDefaultUILanguage`)
uma vez, quando abre — hoje só tem `pt` e `en` (ver `i18n.py`), qualquer outro
idioma do Windows cai em inglês. Se você mudar o idioma do Windows, é só reabrir
o widget que ele detecta o novo. Pra adicionar um idioma, é só acrescentar uma
entrada no dicionário `STRINGS` em `i18n.py` com as mesmas chaves que já existem.

## Segurança (nenhuma chave sua vai junto do código)

Nada aqui tem chave de API, token ou senha escrito no código-fonte:

- O `OAUTH_CLIENT_ID` no `tray_widget.py` é público — é o mesmo id que o próprio
  Claude Code CLI usa, não é secreto, não identifica você.
- O token de verdade (`accessToken`/`refreshToken`) só existe em
  `~/.claude/.credentials.json`, **fora da pasta do projeto**, lido em tempo de
  execução. Se você mandar essa pasta pra alguém (zip, pen drive, GitHub — o que
  for), ela não carrega esse arquivo junto, e a outra pessoa não teria acesso à
  sua conta com o código sozinho.
- Todo arquivo de estado/cache/log que o widget cria (`usage-cache.json`,
  `usage-widget-state.json`, etc.) também fica em `~/.claude/`, nunca dentro da
  pasta do projeto — então copiar/compartilhar a pasta `claude-usage-widget/` não
  arrasta nenhum dado de uso seu junto.
- Incluí um `.gitignore` já preparado, caso você resolva versionar isso com git
  em algum momento.

## Compartilhando com outras pessoas

Como cada pessoa tem seu próprio `~/.claude/.credentials.json` e seus próprios
logs, o código já funciona pra qualquer pessoa com Claude Code Pro no Windows
sem precisar mudar nada — quem rodar vai ver os números da própria conta dela,
não a sua. Duas formas de entregar:

- **Só a pasta/zip** (mais simples): a pessoa roda `pip install -r
  requirements.txt` e depois `python tray_widget.py`, ou você já manda o `.exe`
  gerado pelo `build.ps1` (aí nem precisa ter Python instalado).
- **Repositório Git** (se quiser deixar público ou fácil de atualizar pra várias
  pessoas): dá pra `git init` nesta pasta — o `.gitignore` já protege contra
  arquivo de credencial ser commitado por engano.

## Arquivos de estado (todos em `~/.claude/`, não precisam ser versionados)

- `usage-cache.json` — último dado de quota conhecido (statusline ou fallback).
- `usage-credits-cache.json` — último dado real de créditos de uso (CR$).
- `usage-widget-state.json` — offsets de leitura incremental dos logs + custo
  acumulado do mês, por arquivo.
- `usage-widget.lock` — trava de instância única.
- `usage-widget-debug.log` — só recebe linhas quando algo falha silenciosamente
  (ex. fallback de API indisponível). Vazio é sinal de que está tudo ok.
