# voicedictate

Ditado por voz para Linux. Pressione `Alt+Z` para começar a gravar, pressione de novo para parar — o texto aparece onde o cursor estiver.

---

## Instalação (primeira vez)

```bash
cd ~/Dev/voicedictate
bash setup.sh
```

O script vai:
- Instalar dependências do sistema (`xdotool`, `portaudio`, etc.)
- Adicionar seu usuário ao grupo `input` (necessário para ler o teclado)
- Criar um ambiente Python isolado em `~/.local/share/voicedictate/venv`
- Baixar o modelo Whisper `base` (~150 MB, multilíngue)
- Instalar o comando `voicedictate` em `~/.local/bin/`
- Configurar um serviço systemd que inicia automaticamente com o desktop

> **Após o setup, faça logout e login** para o grupo `input` entrar em vigor.

---

## Como usar

1. **Pressione** `Alt+Z` → beep agudo → comece a falar
2. **Pressione** `Alt+Z` de novo → beep grave → aguarde a transcrição
3. O texto aparece digitado automaticamente onde o cursor estiver

---

## Iniciar / parar o serviço

```bash
# Iniciar
systemctl --user start voicedictate

# Parar
systemctl --user stop voicedictate

# Ver se está rodando
systemctl --user status voicedictate

# Ver logs em tempo real
journalctl --user -u voicedictate -f
```

O serviço é habilitado para iniciar automaticamente com o desktop. Após reinstalar ou atualizar, rode:

```bash
systemctl --user daemon-reload
systemctl --user restart voicedictate
```

---

## Testar sem serviço (útil para debug)

Se ainda não fez logout/login após o setup, use este atalho para ativar o grupo `input` temporariamente:

```bash
systemctl --user stop voicedictate   # parar o serviço antes
sg input -c "~/.local/bin/voicedictate"
```

---

## Configurações

As configurações são feitas por variáveis de ambiente — sem precisar editar código.

| Variável               | Padrão                          | Descrição                                     |
|------------------------|---------------------------------|-----------------------------------------------|
| `VOICEDICTATE_MOD`     | `KEY_LEFTALT,KEY_RIGHTALT`      | Modificador(es) da hotkey (qualquer Alt)      |
| `VOICEDICTATE_KEY`     | `KEY_Z`                         | Tecla principal da hotkey                     |
| `VOICEDICTATE_MODEL`   | `base`                          | Modelo Whisper (ver lista abaixo)             |
| `VOICEDICTATE_LANG`    | `pt`                            | Código do idioma (`pt`, `en`, `auto`)         |
| `VOICEDICTATE_PROMPT`  | _(vazio)_                       | Prompt inicial do Whisper (nome, vocabulário) |
| `DISPLAY`              | `:0`                            | Display X11 (raramente precisa mudar)         |

### Teclas principais disponíveis (`VOICEDICTATE_KEY`)

| Valor         | Tecla física |
|---------------|--------------|
| `KEY_Z`       | Z (padrão)   |
| `KEY_SPACE`   | Espaço       |
| `KEY_F9`      | F9           |
| `KEY_GRAVE`   | ` (backtick) |

### Modificadores disponíveis (`VOICEDICTATE_MOD`)

| Valor                           | Tecla física                     |
|---------------------------------|----------------------------------|
| `KEY_LEFTALT,KEY_RIGHTALT`      | Qualquer Alt (padrão)            |
| `KEY_LEFTCTRL,KEY_RIGHTCTRL`    | Qualquer Ctrl                    |
| `KEY_LEFTMETA`                  | Super (tecla Windows) esquerda   |

### Modelos Whisper

Modelos multilíngues (suportam português):

| Modelo   | Tamanho | Velocidade | Qualidade       |
|----------|---------|------------|-----------------|
| `tiny`   | ~39 MB  | Muito rápido | Básica         |
| `base`   | ~74 MB  | Rápido     | Boa (**padrão**) |
| `small`  | ~244 MB | Médio      | Ótima           |
| `medium`         | ~769 MB | Lento        | Excelente           |
| `large-v3-turbo` | ~809 MB | Moderado     | Máxima (**recomendado para CUDA**) |
| `large-v3`       | ~1.5 GB | Lento        | Máxima              |

> Não use modelos com sufixo `.en` (ex: `base.en`) — eles só entendem inglês.

### Exemplos de configuração

Trocar para `Ctrl+Space`:
```bash
VOICEDICTATE_MOD=KEY_LEFTCTRL,KEY_RIGHTCTRL VOICEDICTATE_KEY=KEY_SPACE voicedictate
```

Usar modelo `small` para mais precisão em português:
```bash
VOICEDICTATE_MODEL=small voicedictate
```

Detectar idioma automaticamente:
```bash
VOICEDICTATE_LANG=auto voicedictate
```

Informar contexto ao modelo para melhorar precisão (nomes, termos técnicos):
```bash
VOICEDICTATE_PROMPT="Otávio, engenheiro de software, fala sobre Python, Linux e IA." voicedictate
```

Para tornar permanente, edite o serviço systemd:
```bash
systemctl --user edit voicedictate
```
E adicione:
```ini
[Service]
Environment=VOICEDICTATE_MOD=KEY_LEFTALT,KEY_RIGHTALT
Environment=VOICEDICTATE_KEY=KEY_Z
Environment=VOICEDICTATE_MODEL=small
```
Depois:
```bash
systemctl --user restart voicedictate
```

---

## Re-baixar o modelo após troca

Se mudar o modelo (ex: de `base` para `small`), ele é baixado automaticamente na primeira execução. Para forçar o download antecipado:

```bash
~/.local/share/voicedictate/venv/bin/python -c "
from faster_whisper import WhisperModel
WhisperModel('small', device='cpu', compute_type='int8')
print('Modelo baixado.')
"
```

---

## Problemas comuns

### "No keyboard found"
Você ainda não está no grupo `input` nesta sessão. Faça logout e login, ou use `sg input -c "~/.local/bin/voicedictate"` para testar sem reiniciar.

### O texto não aparece em algum app
`xdotool` só funciona em X11. Se você estiver em Wayland, o daemon precisa de ajuste (abra uma issue).

### A transcrição está em inglês
Certifique-se de usar um modelo sem `.en` (use `base`, não `base.en`) e que `VOICEDICTATE_LANG=pt`.

### O serviço trava em loop de reinicialização
Pare primeiro e veja o log:
```bash
systemctl --user stop voicedictate
journalctl --user -u voicedictate -n 30 --no-pager
```
