# Backtester GBP/USD — świeca sesyjna 8:00–8:15

Aplikacja webowa, która testuje na danych historycznych taką strategię:

> Patrzymy na świecę 15-minutową od **8:00 do 8:15**. Jeśli jest **zielona** (wzrostowa),
> otwieramy pozycję **długą**; jeśli **czerwona** (spadkowa) — **krótką**. Take profit ustawiony
> jest **4 razy dalej niż stop loss** (RR 4:1). Pozycja nie ma limitu czasowego — żyje aż
> trafi TP albo SL. Tak sprawdzany jest każdy dzień z historii.

To jest **domyślne ustawienie**, a nie sztywna reguła — w panelu po lewej stronie można zmienić
każdy element tej logiki: godzinę i długość świecy, kierunek, sposób liczenia stop lossa,
stosunek RR, zarządzanie pozycją, kapitał, dźwignię i zakres testowanych dni.

## Uruchomienie

```bash
./run.sh
```

Skrypt wygeneruje dane demo, doinstaluje zależności i wystartuje serwer na
<http://127.0.0.1:8000>. Potrzebny jest Python 3.11 lub nowszy.

Ręcznie, jeśli wolisz:

```bash
python3 -m pip install -r requirements.txt
python3 tools/make_sample_data.py          # tylko za pierwszym razem
python3 -m uvicorn app.main:app --port 8000
```

## Skąd wziąć dane

### Eksport z TradingView (zalecane)

TradingView nie udostępnia publicznego API do pobierania historii, więc dane trzeba
wyeksportować ręcznie — za to są to dokładnie te kwotowania, które widzisz na swoim wykresie:

1. Otwórz wykres GBP/USD i ustaw interwał **15 minut**.
2. Przewiń wykres w lewo tak daleko, ile dni chcesz przetestować (TradingView eksportuje tylko
   to, co jest wczytane na wykresie).
3. Menu **„…"** w prawym górnym rogu → **Eksportuj dane wykresu** → format CSV.
4. Wgraj pobrany plik w sekcji **1 · Dane**.

Parser radzi sobie sam z wariantami formatu: separator `,`, `;` lub tabulator, przecinek albo
kropka dziesiętna, czas jako unix (sekundy/milisekundy), ISO 8601, `DD.MM.RRRR GG:MM`,
`RRRRMMDD GGMMSS` (HistData) oraz data i godzina w dwóch osobnych kolumnach (eksport z MT4/MT5).
Wiersze niespójne (np. `high` poniżej `open`) są odrzucane, a ich liczba raportowana.

**Ważne — strefa czasowa.** Jeśli plik nie zawiera informacji o strefie, godziny są
interpretowane w strefie wybranej w polu „Strefa czasowa wykresu". Ustaw tam tę samą strefę,
którą masz ustawioną w TradingView, inaczej „8:00" będzie oznaczać inną świecę.

### Przycisk „Pobierz z sieci"

Pobiera około 60 dni danych 15-minutowych z publicznego API Yahoo Finance. Wygodne na szybki
rzut oka, ale to **inny dostawca kwotowań niż TradingView** — świece mogą się nieznacznie różnić.

> Ta ścieżka nie została przetestowana w środowisku, w którym powstawał projekt, bo polityka
> sieciowa blokowała tam zewnętrzne hosty. Kod obsługuje błąd czytelnym komunikatem, ale samo
> pobieranie zadziała dopiero na maszynie z otwartym internetem.

### Inne instrumenty niż GBP/USD

Silnik nie wie i nie musi wiedzieć, jaki instrument liczy — przetwarza po prostu świece OHLC.
Sprawdzone na EUR/USD, USD/JPY, EUR/GBP, złocie, indeksie i bitcoinie: wszystko liczy się
tak samo. Trzy rzeczy warto ustawić świadomie:

- **Rozmiar pipsa** dopasowuje się automatycznie po wczytaniu pliku (0,0001 dla par walutowych,
  0,01 dla par z jenem, 0,1 dla złota i indeksów, 1 dla krypto). Możesz go nadpisać. Wpływa
  wyłącznie na metodę „stałe pipsy" i na spread — przy stopie z zakresu świecy albo z procentu
  ceny nie ma żadnego znaczenia.
- **Dni tygodnia.** Krypto handluje się siedem dni w tygodniu, więc zaznacz sobotę i niedzielę
  — inaczej stracisz jakieś 28% sygnałów.
- **Waluta wyniku.** Zysk i strata wychodzą w **walucie kwotowanej**, czyli tej po prawej
  stronie pary. Dla GBP/USD, EUR/USD czy XAU/USD jest to USD i przy koncie dolarowym wszystko
  się zgadza. Ale przy **USD/JPY wynik jest w jenach**, a przy **EUR/GBP w funtach** — żeby
  dostać kwotę w walucie konta, trzeba go jeszcze przeliczyć po kursie. Procenty i statystyki
  (skuteczność, profit factor, obsunięcie) są poprawne niezależnie od pary.

Przycisk „Pobierz z sieci" ma pole na symbol w notacji Yahoo Finance — `EURUSD=X`, `USDJPY=X`,
`BTC-USD`, `^GSPC` i tak dalej. Wgrany plik CSV działa dla dowolnego instrumentu bez ograniczeń.

### Długa historia — czy da się przetestować 10 lat wstecz

Po stronie aplikacji tak, i to bez zadyszki. Zmierzone na 10 latach danych 15-minutowych
(250 560 świec, plik 16 MB): wczytanie pliku 3,3 s, sam backtest 0,2–0,9 s zależnie od trybu,
2 610 dni sygnałowych w tabeli. Limit wgrywanego pliku to 64 MB, więc miejsca jest z zapasem.
Tabela wyników jest stronicowana, żeby sortowanie tysięcy wierszy pozostało natychmiastowe.

Prawdziwym ograniczeniem jest **zdobycie takich danych**. TradingView eksportuje tylko to, co
jest wczytane na wykresie, a ile świec da się wczytać, zależy od planu — na darmowym koncie
zwykle kilka tysięcy, na płatnych więcej, ale i tak znacznie mniej niż ćwierć miliona świec
potrzebnych na 10 lat interwału 15-minutowego. Przycisk „Pobierz z sieci" odpada tym bardziej:
Yahoo oddaje dla 15 minut najwyżej około 60 dni.

Na naprawdę długą historię sięgnij po źródło, które daje pełne archiwum:

| Źródło | Co daje | Format |
|---|---|---|
| **HistData.com** | Darmowe dane 1-minutowe dla par walutowych, miesiąc po miesiącu, wstecz do ~2000 roku | `RRRRMMDD GGMMSS;O;H;L;C;V` — obsługiwany |
| **Eksport z MT4/MT5** | Historia od Twojego brokera (Narzędzia → Centrum historii) | data i godzina w osobnych kolumnach — obsługiwany |
| **Dukascopy** | Darmowe dane tickowe i minutowe z długim archiwum | po konwersji do CSV z kolumnami OHLC |

Dane 1-minutowe są tu nawet **lepsze niż 15-minutowe**: aplikacja sama złoży z nich świecę
sygnałową 8:00–8:15, a drobniejszych świec użyje do symulacji wyjść, więc dokładniej wiadomo,
czy pierwszy został trafiony stop loss czy take profit. Kosztem jest rozmiar pliku — 10 lat
danych 1-minutowych to około 3,7 mln świec i grubo ponad 200 MB, czyli powyżej limitu; w takim
wypadku wgrywaj krótsze okresy albo przekonwertuj dane do 5 lub 15 minut.

Jeszcze jedna uwaga o długiej historii: im dalej wstecz, tym mniej wynik mówi o dzisiejszym
rynku. Kurs GBP/USD chodził w 2016 roku w zupełnie innym reżimie zmienności niż teraz, a spread
i koszty finansowania też były inne. Warto porównać wynik z ostatnich 2–3 lat z wynikiem
z całej dostępnej historii — jeśli mocno się różnią, to sygnał, że strategia zależy od reżimu,
a nie od trwałej przewagi.

### Pełna historia z Dukascopy

Sekcja **„Pełna historia z Dukascopy"** w panelu danych (domyślnie zwinięta) pobiera dane
prosto z darmowego archiwum Dukascopy: **dane tickowe sięgające 2003 roku, bez konta i bez
limitów planu**. To jedyna opcja w tej aplikacji, która nie jest niczym ścięta — TradingView
ogranicza eksport tym, ile świec wczyta wykres, a Yahoo oddaje dla 15 minut jakieś 60 dni.

Jak to działa: Dukascopy trzyma po jednym spakowanym pliku na każdą godzinę handlu.
Aplikacja pobiera je równolegle, rozpakowuje, odczytuje ticki i **sama składa z nich świece**
o wybranym interwale. Dzięki temu możesz zejść nawet do świec 1-minutowych, co daje
dokładniejsze wyjścia z pozycji (patrz „Ograniczenia backtestu" niżej).

- Pobieranie idzie w tle, z paskiem postępu i możliwością przerwania — wieloletni zakres
  nie zablokuje przeglądarki.
- Pobrane godziny lądują w pamięci podręcznej na dysku (`data/dukascopy_cache/`), więc
  ponowne pobranie tego samego okresu jest natychmiastowe, a przerwane pobieranie da się wznowić.
- Świece budowane są po cenie **bid**, tak jak wykresy walutowe pokazuje TradingView.
- Tempo: miesiąc to kilkanaście sekund, rok kilka minut. Zacznij od krótkiego zakresu,
  żeby sprawdzić połączenie.

Dostępne instrumenty: GBP/USD (domyślnie), EUR/USD, USD/JPY, EUR/GBP, AUD/USD, USD/CHF,
USD/CAD i złoto.

> Sama warstwa sieciowa nie została zweryfikowana w środowisku, w którym powstawał projekt —
> polityka egress blokowała tam `datafeed.dukascopy.com`. Przetestowane jest natomiast wszystko
> poza samym gniazdem sieciowym: dekoder formatu `.bi5`, skalowanie cen, składanie ticków
> w świece, pamięć podręczna, raportowanie postępu i obsługa błędów — na plikach budowanych
> w testach oraz przez uruchomienie całej aplikacji z podstawioną warstwą pobierania.

### Czego NIE da się zrobić: pobieranie historii z TradingView

Dla porządku, bo to częste nieporozumienie: **TradingView nie udostępnia żadnego publicznego
API do pobierania historii świec.** Biblioteki i serwery MCP z „tradingview" w nazwie
(np. `tradingview-ta`, `tradingview-screener`) korzystają z endpointu
`scanner.tradingview.com`, który zwraca **migawkę wskaźników** — bieżące RSI, MACD,
rekomendację kup/sprzedaj dla symbolu. Nie ma tam serii czasowej i nie da się z tego zbudować
świec. Narzędzia, które reklamują się backtestem, i tak biorą świece z Yahoo Finance —
czyli stamtąd, skąd bierze je przycisk „Pobierz z sieci", z tymi samymi ograniczeniami.

Jeśli szukasz sposobu na ominięcie limitów planu TradingView: nie ma czego omijać, bo nie ma
skąd tych danych wziąć. Zamiast tego użyj Dukascopy albo HistData — dają **więcej** historii
niż TradingView Premium.

### Dane demo

Przycisk **„Dane demo"** ładuje wbudowany plik z pół roku świec 15-minutowych. Są to dane
**syntetyczne** — losowe błądzenie z ustalonym ziarnem, wygenerowane przez
`tools/make_sample_data.py`. Służą wyłącznie do sprawdzenia, jak działa aplikacja.
**Nie wyciągaj z nich żadnych wniosków o rynku.**

## Co znaczy które ustawienie

### 2 · Świeca sygnałowa

| Pole | Znaczenie |
|---|---|
| Początek świecy | Godzina otwarcia świecy sygnałowej (domyślnie 08:00). |
| Długość | Ile minut trwa świeca sygnałowa (domyślnie 15). |
| Kierunek pozycji | `Podążaj za świecą` = pierwotna logika. Można ją odwrócić albo ograniczyć do samych longów lub shortów. |
| Gdy świeca bez zmiany | Co zrobić, gdy otwarcie równa się zamknięciu (doji). |

### 3 · Logika pozycji

| Pole | Znaczenie |
|---|---|
| Zysk do ryzyka (RR) | Ile razy dalej leży take profit niż stop loss. 4 = pierwotna strategia. |
| Wejście | Po otwarciu kolejnej świecy (czyli o 8:15) albo po zamknięciu świecy sygnałowej. |
| Skąd brać dystans SL | **Zakres świecy** — stop na dołku świecy 8:00–8:15 (dla longa) lub na jej szczycie (dla shorta). **Stałe pipsy** — zawsze ta sama odległość. **Procent ceny** — odległość jako % ceny wejścia. |
| Mnożnik SL | Rozszerza lub zawęża wyliczony dystans (2 = dwa razy dalszy stop). |
| Spread | Koszt spreadu w pipsach, doliczany raz, przy wejściu. |
| Rozmiar pipsa | 0,0001 dla GBP/USD. Zmień przy innym instrumencie. |
| Gdy świeca dotyka i SL, i TP | Której cenie wierzyć, gdy jedna świeca obejmuje oba poziomy. Domyślnie zakładany jest gorszy scenariusz (stop loss). |

### 4 · Zarządzanie pozycją

Pozycja domyślnie nie ma limitu czasowego. To pole decyduje, co się dzieje, gdy o 8:00
pojawia się nowy sygnał, a poprzednia pozycja wciąż żyje:

| Tryb | Zachowanie |
|---|---|
| **Równolegle, bez limitu czasu** | Każdy dzień dostaje własną pozycję; kilka może być otwartych naraz. Ustawienie domyślne. |
| **Zamknij o określonej godzinie** | Dodatkowa reguła czasowa — pozycja zamykana o podanej godzinie, po cenie otwarcia świecy. Pole „po ilu dniach": 0 = tego samego dnia. Trafiony wcześniej TP lub SL ma pierwszeństwo. |
| **Zamknij starą przy otwarciu nowej** | Nowe wejście zamyka poprzednią pozycję po tej samej cenie. Nigdy więcej niż jedna pozycja. |
| **Pomiń nowy sygnał** | Sygnał jest ignorowany, dopóki poprzednia pozycja się nie zamknie. |

### 5 · Kapitał i dźwignia

| Tryb wielkości pozycji | Jak liczony jest nominał |
|---|---|
| **Cała ekspozycja, z kapitalizacją** | `bieżący kapitał × dźwignia`. Zyski powiększają kolejne pozycje, straty je zmniejszają. |
| **Cała ekspozycja, bez kapitalizacji** | `kapitał początkowy × dźwignia`. Każda transakcja tej samej wielkości — łatwiej je porównywać. |
| **Stałe ryzyko % na transakcję** | Wielkość liczona wstecz z odległości stop lossa tak, aby strata wyniosła zadany % kapitału. Dźwignia działa wtedy jako górny limit; przycięcie jest oznaczane. |

Przy trybie równoległym ekspozycja otwartych pozycji **sumuje się**. Podsumowanie pokazuje
maksymalną liczbę pozycji naraz i szczytową efektywną dźwignię — jeśli ta druga urośnie, dostaniesz
ostrzeżenie. Gdy kapitał spadnie do zera, backtest zatrzymuje się ze statusem likwidacji.

### 6 · Zakres testu

Liczba dni wstecz (0 = cała historia, liczone w dniach kalendarzowych od ostatniej świecy),
opcjonalny zakres dat oraz filtr dni tygodnia.

## Jak czytać wyniki

Na samej górze strony: kumulatywna stopa zwrotu procentowo i w pieniądzu, kapitał końcowy,
skuteczność, liczba dni na plus i na minus, maksymalne obsunięcie, profit factor, średnia
transakcja i szczytowa ekspozycja.

Niżej — wykres kumulatywnej stopy zwrotu (najedź kursorem, żeby zobaczyć konkretny punkt),
tabela wyników w rozbiciu na dni tygodnia oraz pełna tabela wszystkich dni: data, dzień tygodnia,
kierunek, ceny wejścia, SL i TP, moment i powód wyjścia, wynik w pieniądzu i procentowo oraz
stopa zwrotu narastająco. Tabelę można sortować po każdej kolumnie i wyeksportować do CSV.

Dni pominięte też są w tabeli, razem z powodem — na przykład „pozycja z wcześniejszego dnia była
jeszcze otwarta" albo „dystans Stop Lossa wyszedł zerowy lub ujemny".

Kolumna **narastająco** rośnie w kolejności *zamykania* pozycji, bo dopiero wtedy zmienia się
kapitał. Przy nakładających się pozycjach kolejność w tej kolumnie może więc różnić się od
kolejności dat sygnałów.

## Ograniczenia backtestu

Warto je znać, zanim potraktujesz wynik poważnie:

- **Brak punktów swapowych.** Pozycje trzymane przez noc są w rzeczywistości obciążane
  (lub premiowane) odsetkami. Tu tego nie ma, a strategia bez limitu czasu potrafi trzymać
  pozycję tygodniami.
- **Brak prowizji i poślizgów.** Zakłada się, że zlecenie wykonuje się dokładnie po cenie
  stop lossa lub take profitu.
- **Spread stały** i naliczany raz, przy wejściu. W realnym rynku rozszerza się przy publikacji
  danych — czyli dokładnie w okolicach otwarcia sesji londyńskiej.
- **Rozdzielczość danych.** Przy świecach 15-minutowych nie wiadomo, w jakiej kolejności cena
  odwiedziła poziomy wewnątrz jednej świecy. Domyślnie zakładany jest gorszy wariant. Wgranie
  danych 1- lub 5-minutowych daje dokładniejszy wynik — aplikacja sama złoży z nich świecę
  sygnałową i użyje drobniejszych świec do symulacji wyjść.
- **Brak wezwań do uzupełnienia depozytu.** Realny broker zamknąłby część pozycji, zanim kapitał
  dojdzie do zera.

Wyniki historyczne nie są obietnicą wyników przyszłych.

## Struktura projektu

```
app/
  config.py       konfiguracja backtestu, walidacja, wartości domyślne
  csv_loader.py   normalizacja plików CSV z TradingView
  engine.py       silnik backtestu
  stats.py        metryki i rozbicie na dni tygodnia
  fetch.py        opcjonalne pobieranie danych z Yahoo Finance
  dukascopy.py    pobieranie archiwum tickowego i składanie go w świece
  main.py         serwer HTTP i API
web/              frontend (HTML, CSS, czysty JavaScript — bez zależności)
tools/            generator danych demo
tests/            testy jednostkowe
```

## Testy

```bash
python3 -m pytest tests/ -q
```

Testy sprawdzają parser CSV (warianty formatu czasu, separatory, odrzucanie złych wierszy),
logikę kierunku, wszystkie warianty wyjścia z pozycji, trzy metody stop lossa, trzy tryby
wielkości pozycji, wszystkie cztery tryby zarządzania pozycją, poprawność stref czasowych
oraz zatrzymanie backtestu przy wyzerowaniu kapitału.

## API

Frontend korzysta z tych samych endpointów, więc można je wołać skryptem:

| Endpoint | Opis |
|---|---|
| `GET /api/options` | Listy opcji i wartości domyślne. |
| `POST /api/upload` | Wgranie pliku CSV (multipart). Zwraca `dataset_id`. |
| `GET /api/sample` | Wczytanie danych demo. |
| `POST /api/fetch` | Pobranie danych z Yahoo Finance. |
| `POST /api/dukascopy/start` | Start pobierania z Dukascopy w tle. Zwraca `job_id`. |
| `GET /api/dukascopy/status/{job_id}` | Postęp pobierania, a po zakończeniu gotowy zbiór danych. |
| `POST /api/dukascopy/cancel/{job_id}` | Przerwanie pobierania. |
| `POST /api/backtest` | `{"dataset_id": "...", "config": {...}}` → pełne wyniki. |
