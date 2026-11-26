# Roteiro de execução: imagem → vetor → .PES (Wilcom Embroidery Studio)

## O que o processo faz

`image_to_pes.py` recebe uma imagem (JPG ou PNG) e gera o arquivo de bordado:

1. **Quantização de cores**: imagens pequenas (menos de 2000 px no maior lado) são ampliadas antes, para os contornos saírem mais suaves. Depois a imagem é reduzida a N cores chapadas (k-means), usando as cores da própria imagem.
2. **Fundo, bordas e recorte**: a cor que mais aparece na borda da imagem é tratada como fundo e não é bordada. Pixels serrilhados de borda que formariam um "halo" de outra cor em volta das letras são corrigidos. As margens vazias são recortadas, então o tamanho definido vale para o desenho em si.
3. **Vetorização**: cada cor vira polígonos (contornos e furos) suavizados e simplificados. O resultado também é salvo como SVG.
4. **Digitalização**: cada polígono recebe underlay duplo (contorno interno, que segura as bordas, e tatami cruzado, que dá corpo) e preenchimento tatami denso (0,30 mm entre linhas), com compensação de repuxo e corte de linha nos deslocamentos.
5. **Saída**: grava o `.pes` (PES v6, abre no Wilcom e em máquinas Brother/Babylock) e um PNG de prévia que desenha os pontos com a espessura real da linha.

## 1. Preparar o ambiente (uma única vez)

```bash
cd /caminho/da/pasta/do/projeto
python3 -m venv .venv            # se o venv não estiver disponível: pip install --user virtualenv && python3 -m virtualenv .venv
source .venv/bin/activate        # no Windows: .venv\Scripts\activate
pip install -r dependencies.txt
```

## 2. Converter uma imagem

Coloque a imagem na pasta `./entrada/` e passe só o nome do arquivo:

```bash
python image_to_pes.py <nome_da_imagem>
```

O script procura primeiro em `./entrada/`. Se não achar, usa o nome como caminho normal, então ainda dá para apontar para um arquivo em outro lugar (ex.: `fotos/logo.png`). Para usar outra pasta de entrada: `-i <pasta>`.

Os arquivos de saída usam o nome da imagem e ficam em `./saida/`:

| Arquivo                         | Conteúdo                                                   |
|---------------------------------|------------------------------------------------------------|
| `saida/<nome>.pes`              | arquivo de bordado para o Wilcom ou a máquina              |
| `saida/<nome>_vetor.svg`        | imagem vetorizada, para conferir (também pode ser importada no Wilcom) |
| `saida/<nome>_preview.png`      | prévia dos pontos gerados                                  |

Exemplo com a imagem `entrada/imagem.jpeg`:

```bash
python image_to_pes.py imagem.jpeg
# Imagem: 1536x1024 px (ampliada 2x para vetorizar)
# Pixels de borda (halo) corrigidos: 17331
# Bordado: 150.0 x 81.6 mm
# Fundo removido: #feffff
# Cor #de2568: 8 formas
# Cor #07090b: 39 formas
# SVG vetorizado: saida/imagem_vetor.svg
# PES gerado: saida/imagem.pes (8546 pontos, 2 cores)
# Prévia: saida/imagem_preview.png
```

Outros exemplos:

```bash
# outra pasta de saída
python image_to_pes.py fotos/logo_cliente.png -d entregas/cliente_x

# caminho e nome explícitos para o .pes
python image_to_pes.py fotos/logo.png -o entregas/logo_bordado.pes

# bordado com 100 mm de largura, 4 cores (contando o fundo) e contorno em ponto corrido
python image_to_pes.py fotos/logo.png -w 100 -c 4 --outline
```

## 3. Parâmetros

| Parâmetro            | Padrão  | Para que serve |
|----------------------|---------|----------------|
| `-i, --input-dir`    | `entrada` | pasta onde as imagens de entrada são procuradas |
| `-d, --output-dir`   | `saida` | pasta onde os arquivos são gravados |
| `-o, --output`       | (nenhum) | caminho do `.pes`; os outros arquivos ficam ao lado com o mesmo nome |
| `-c, --colors`       | `3`     | número de cores, **contando o fundo**. Logo com 2 cores sobre fundo branco usa 3 |
| `-w, --width-mm`     | `150`   | largura final do desenho em mm, sem contar as margens vazias da imagem |
| `-H, --max-height-mm`| `150`   | altura máxima em mm. Se o desenho passar disso, é reduzido mantendo a proporção (0 = sem limite) |
| `--no-crop`          | desligado | não recorta as margens vazias; aí `-w` vale para a imagem inteira |
| `--min-size`         | `2000`  | amplia imagens cujo maior lado tenha menos que isso em px (0 = não amplia) |
| `--keep-background`  | desligado | borda também a cor de fundo |
| `--angle`            | `45`    | ângulo do preenchimento em graus |
| `--density`          | `0.30`  | espaçamento entre as linhas do preenchimento em mm. Menor fica mais denso; faixa útil de 0,25 a 0,45 |
| `--underlay-spacing` | `1.5`   | espaçamento do underlay tatami em mm |
| `--stitch-len`       | `3.0`   | comprimento do ponto do preenchimento em mm |
| `--pull-comp`        | `0.25`  | aumenta as formas em mm para compensar o repuxo do tecido |
| `--outline`          | desligado | adiciona contorno em ponto corrido, o que deixa as bordas mais nítidas |
| `--no-underlay`      | desligado | não gera a base (contorno interno e tatami) sob o preenchimento |
| `--min-area`         | `0.2`   | descarta formas e furos menores que essa área em mm² |
| `--smooth`           | `2`     | suavização das bordas em pixels, antes de vetorizar |
| `--simplify`         | `0.8`   | tolerância da simplificação dos contornos em pixels |
| `--max-travel`       | `1.0`   | deslocamentos maiores que isso (mm) viram corte de linha e salto |
| `--no-svg`, `--no-preview` | desligado | não gravam o SVG ou o PNG de prévia |

## 4. Abrir no Wilcom

1. `File > Open` e escolha `saida/<nome>.pes`.
2. Troque as cores das linhas na paleta (Color Palette) conforme o tecido e as linhas disponíveis.
3. **Tamanho:** um PES é aberto no Wilcom como pontos, não como objetos editáveis. Reduzir ou ampliar mais de ±10–20% pode deixar o bordado denso ou ralo demais. Para outro tamanho, gere de novo com `-w <mm>`. Outra opção é ativar o recálculo de pontos do Wilcom ao redimensionar.
4. **Alternativa:** importe `saida/<nome>_vetor.svg` (`File > Import Graphic`) e use o auto-digitize do Wilcom sobre o vetor limpo.

## 5. Dicas de qualidade

- **Comece com a imagem certa:** logos e artes com cores chapadas e fundo liso funcionam melhor. Fotos precisam de `-c` maior e normalmente não ficam boas como bordado.
- **Texto pequeno:** traços com menos de ~1 mm no tamanho final ficam ilegíveis. Na imagem de exemplo, "Mais saúde, mais vida!" tem traço de ~0,6 mm a 150 mm de largura. Para melhorar, aumente `-w`, use `--outline` ou refaça esse texto com uma fonte de bordado no Wilcom.
- **Cor faltando ou sobrando:** ajuste `-c`. Uma cor de borda serrilhada entre duas cores indica que `-c` está alto demais.
- **Densidade e tecido:** o padrão de 0,30 mm dá cobertura fechada em tecidos estáveis (brim, sarja, piquê). Em malha fina ou tecido leve, use `--density 0.35` ou `0.40` com entretela, para não franzir. Abaixo de 0,25 mm aumenta o risco de quebrar agulha e de o bordado ficar duro.
- **Contraforma em letras pequenas:** a compensação de repuxo engorda as formas. Se o "miolo" de letras pequenas fechar, reduza `--pull-comp` (por exemplo `0.1`).
- **Bastidor:** o padrão cabe numa área de 150 × 150 mm. Para outro bastidor, ajuste `-w` e `-H`. Exemplo para 100 × 100 mm: `-w 100 -H 100`.
- **Imagem de baixa resolução:** imagens pequenas (por exemplo 512 px de largura) são ampliadas automaticamente, mas texto muito pequeno no original continua sem detalhe. Use a imagem de maior resolução que tiver.
- **Confira antes de bordar:** veja `_preview.png` (ou o simulador do Wilcom) e faça um teste em retalho.
