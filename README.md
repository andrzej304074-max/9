# Backtester GBP/USD — świeca sesyjna 8:00–8:15

Aplikacja webowa testująca na danych historycznych **dwie niezależne strategie** oparte na tej
samej świecy 15-minutowej od 8:00 do 8:15. Przełącznik na górze panelu decyduje, którą liczymy;
każda ma własny komplet ustawień i własną pamięć, a przycisk **„Porównaj obie strategie"**
liczy je naraz na tych samych danych i pokazuje obie krzywe kapitału na jednym wykresie.

### Strategia 1 — kierunek świecy

> Patrzymy na świecę od **8:00 do 8:15**. Jeśli jest **zielona** (wzrostowa), otwieramy pozycję
> **długą**; jeśli **czerwona** (spadkowa) — **krótką**. Wejście zaraz po jej zamknięciu.
> Take profit **4 razy dalej niż stop loss** (RR 4:1). Pozycja nie ma limitu czasowego.

### Strategia 2 — wybicie zakresu

> Notujemy **zakres** świecy 8:00–8:15 (jej szczyt i dołek) i **czekamy**, aż w dalszej części
> sesji cena go przebije górą albo dołem. Gramy **w stronę wybicia**, stop loss stawiamy
> po **przeciwnej stronie tej świecy**, a take profit **4 razy dalej** niż stop.

W obu przypadkach to tylko **ustawienia domyślne**, a nie sztywne reguły — w panelu po lewej
można zmienić każdy element: godzinę i długość świecy, kierunek, sposób liczenia stop lossa,
stosunek RR, zarządzanie pozycją, kapitał, dźwignię i zakres testowanych dni.

## Uruchomienie

```bash
./run.sh
```

Skrypt wygeneruje dane demo, doinstaluje zależności i wystartuje serwer na
<http://127.0.0.1:8000>. Potrzebny jest Python 3.11 lub nowszy.

Ręcznie, jeśli wolisz:

```bash
python3 -m pip install -r requirements-dev.txt
python3 tools/make_sample_data.py          # tylko za pierwszym razem
python3 -m uvicorn app.main:app --port 8000
```

## Wdrożenie w chmurze

Aplikację można postawić na Vercelu — pełna instrukcja krok po kroku jest w **[DEPLOY.md](DEPLOY.md)**.

W skrócie: zaimportuj repozytorium na [vercel.com/new](https://vercel.com/new), zostaw wszystkie
ustawienia domyślne i kliknij *Deploy*. Konfigurację niesie plik `vercel.json`.

Środowisko bezserwerowe nakłada trzy ograniczenia, z którymi aplikacja radzi sobie sama, ale
warto o nich wiedzieć:

- **Pobieranie z Dukascopy** samo dzieli się na części: serwer bierze tyle dni, ile zdąży
  w swoim limicie czasu, i mówi, dokąd doszedł, a przeglądarka wznawia od następnego dnia — aż
  do końca zakresu. Nie ma tu żadnego z góry ustalonego rozmiaru części, więc mechanizm sam
  dostosowuje się do prędkości łącza. Lokalnie zamiast tego działa jedno zadanie w tle
  z postępem liczonym co plik.
- **Wgrywany plik** jest pakowany gzipem w przeglądarce; limit 4 MB po kompresji odpowiada
  mniej więcej 20 MB CSV, czyli kilkunastu latom świec 15-minutowych.
- **Pamięć między żądaniami jest ulotna** — gdy żądanie trafi na świeżą instancję, front sam
  wysyła dane ponownie i powtarza operację. Użytkownik widzi tylko dłuższą chwilę oczekiwania.
- **Biblioteka zapisanych zbiorów jest trwała po podpięciu magazynu obiektów** (Vercel Blob,
  dwie minuty konfiguracji — patrz [DEPLOY.md](DEPLOY.md)). Bez niego zapis idzie do katalogu
  tymczasowego i znika razem z uśpieniem instancji; panel mówi o tym wprost.

Do codziennej pracy z wieloletnią historią wygodniejsza jest wersja lokalna.

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

Jeden formularz obsługuje dwa źródła — wybierasz je polem **„Skąd pobierać"**:

| Źródło | Zasięg | Kiedy używać |
|---|---|---|
| **Yahoo Finance** | zależny od interwału, patrz niżej | szybki rzut oka na ostatnie tygodnie |
| **Dukascopy** | pełne archiwum **od 2003 roku** | wieloletnie testy, dowolny interwał od 1 minuty |

W obu przypadkach podajesz **interwał** i **ile dni wstecz**, i klikasz ten sam przycisk.
Reszta dzieje się sama: Dukascopy pobiera plik po pliku (jeden na godzinę handlu) i przy
wdrożeniu bezserwerowym samo wznawia pobieranie po każdej przerwie na limit czasu.

Przycisk **„Sprawdź połączenie"** (widoczny przy wybranym Dukascopy) pobiera jeden testowy
plik i mówi wprost, czy archiwum jest osiągalne — przydaje się, gdy pobieranie nie rusza.
Raportuje też, czy archiwum udostępnia gotowe świece minutowe: jeśli tak, pobieranie idzie
nimi i jest około dwudziestokrotnie szybsze.

Gdy plik nie przychodzi, aplikacja **liczy powody i nazywa je wprost**: „odpowiedź HTTP 429",
„przekroczony czas oczekiwania", „nieznana nazwa serwera". Powód rozstrzyga o zupełnie różnych
krokach, więc komunikat od razu mówi, co z tym zrobić — limit żądań mija sam, blokada adresu
wymaga pobrania danych lokalnie, a błąd DNS znaczy, że wdrożenie w ogóle nie ma wyjścia do
sieci. Odpowiedź 404 nie jest porażką: to normalne „tego pliku tu nie ma".

Osobno działa **hamulec**. Dwadzieścia cztery żądania naraz z jednego adresu w serwerowni
wyglądają dla darmowego archiwum jak nadużycie i potrafią pójść odmową w całości — stąd
pobieranie, które z laptopa działa, a z wdrożenia w chmurze pada. Po odmowie wszystkie wątki
dostają wspólną pauzę (rosnącą, z uwzględnieniem nagłówka `Retry-After`), a strumień się
zwęża; udane pliki oddają przepustki z powrotem. Pobieranie samo znajduje tempo, które
archiwum akceptuje, zamiast poddać się przy pierwszej fali odmów.

Aplikacja **nie ufa formatowi plików ze świecami na słowo** — nie jest on oficjalnie
udokumentowany. Przed pierwszym użyciem pobiera jedną dobę świec i jedną godzinę ticków
z tej samej doby, składa ticki w świece minutowe i porównuje. Zgadza się — korzysta;
nie zgadza się albo plików nie ma — cicho wraca do ticków. Dzięki temu pomyłka w odczycie
formatu nie może przemycić błędnych cen do wyników.

#### Yahoo Finance — zasięg

Pobiera dane z publicznego API Yahoo Finance. Wybierasz **interwał** i **ile dni wstecz**;
ponieważ jedno żądanie oddaje ograniczony wycinek, dłuższy okres kompletuje się **oknami
wstecz** — najpierw najświeższe, potem coraz starsze, aż do żądanej daty albo do końca
archiwum dostawcy.

Zasięg jest po stronie Yahoo twardo ograniczony i **zależy od interwału**:

| Interwał | Jak głęboko sięga |
|---|---|
| 1 minuta | 30 dni |
| 5 / 15 / 30 minut | 60 dni |
| 1 godzina | 2 lata |
| 1 dzień | pełna dostępna historia |

Świec 15-minutowych starszych niż ~60 dni Yahoo po prostu nie udostępnia — żaden sposób
pytania tego nie obejdzie. **Po wieloletnią historię minutową przełącz źródło na Dukascopy**
albo wgraj eksport CSV z TradingView. Interfejs mówi to wprost, zanim klikniesz pobieranie,
a po pobraniu raportuje, ile dni faktycznie uzbierał.

To **inny dostawca kwotowań niż TradingView** — świece mogą się nieznacznie różnić.

> Ta ścieżka nie została przetestowana na żywym API w środowisku, w którym powstawał projekt,
> bo polityka sieciowa blokowała tam zewnętrzne hosty. Samo cofanie się oknami, sklejanie
> i zachowanie na granicy archiwum są pokryte testami na atrapie dostawcy.

### Biblioteka zapisanych danych

Każdy plik, który raz trafił do aplikacji — wgrany ręcznie, pobrany z Dukascopy czy z Yahoo —
zostaje zapisany i pojawia się w panelu **„Zapisane dane"** nad wynikami. Widać tam nazwę,
liczbę świec, obejmowany okres, interwał i rozmiar, a przy każdej pozycji cztery przyciski:

- **Wczytaj** — wraca do tych danych bez ponownego pobierania z sieci
- **Nazwa** — własna nazwa zamiast nazwy pliku (przetrwa ponowne wczytanie tych samych danych)
- **Pobierz CSV** — wynosi kopię na dysk, do obejrzenia albo poprawienia w arkuszu
- **Usuń** — kasuje wpis i plik

Ten sam plik wczytany dwa razy daje jedną pozycję — identyfikator wynika z treści, więc
powtórki się nie mnożą.

#### Masowe pobranie archiwum

W panelu **„Zapisane dane"** jest sekcja **„Pobierz całe archiwum Dukascopy"**: zaznaczasz
instrumenty, podajesz liczbę lat, długość świecy i cenę, a aplikacja ściąga to wszystko
i zapisuje każdy instrument jako osobny zbiór. Przed startem widać oszacowanie — ile plików,
ile danych i orientacyjnie ile czasu.

Pobranie dziesięciu lat dla kompletu instrumentów to pół miliona plików godzinowych, czyli
grubo ponad godzina pracy. Żadne pojedyncze żądanie tego nie obejmie — a przy wdrożeniu
bezserwerowym limit to kilkadziesiąt sekund. Dlatego praca dzieli się na **krótkie kroki**:
plan pobierania leży w tym samym magazynie co biblioteka i trzyma dla każdego instrumentu
kursor z pierwszym niepobranym dniem. Każdy krok bierze tyle, ile zmieści się w budżecie czasu,
i przesuwa kursor. Wynika z tego kilka wygodnych własności:

- **zamknięcie karty wstrzymuje, nie kasuje** — po powrocie aplikacja sama podejmuje pracę
  od zapisanego kursora,
- **kolejny krok może trafić na inną instancję** i nic z tego nie wynika,
- **„Przerwij" nie odbiera tego, co się udało** — instrumenty domknięte wcześniej zostają
  w bibliotece,
- **powtórka nie pobiera drugi raz tego samego** — raz ściągnięte godziny są w pamięci podręcznej.

Dziury w archiwum nie przerywają pracy. Pojedyncza godzina bez odpowiedzi jest liczona
i pomijana; cała doba, z której nie przyszło nic, też — z tą różnicą, że jest raportowana
osobno, bo to brakujący dzień handlowy, a nie drobna luka. Komunikat „nie udało się pobrać
ani jednego pliku" pojawia się wyłącznie wtedy, gdy **nic** się nie udało po kilkunastu
próbach. Sama doba nie jest tu dobrą miarą: niedziela ma w archiwum tylko trzy godziny
(rynek otwiera się o 21:00), więc jej niepowodzenie to za słaba przesłanka, żeby przerywać
wieloletnie pobieranie.
Kilka martwych dób z rzędu traktujemy jako blokadę (najczęściej chwilowy limit żądań):
instrument kończy się z wyjaśnieniem, a to, co zdążyło przyjść, zostaje w bibliotece.

Odmowa archiwum na jednej dobie też nie kończy instrumentu — taka doba jest pomijana,
kursor idzie dalej, a dopiero **ósma odmowa z rzędu** uznaje pobieranie za nieudane. Dzięki
temu jeden zły dzień w środku dziesięciu lat nie przekreśla całej reszty.

Nieudany instrument da się wznowić przyciskiem **„Ponów nieudane"**: rusza od swojego kursora,
nie od zera, i nie tyka tych, które trafiły już do biblioteki. Od początku zaczyna tylko
instrument, który przeszedł cały zakres bez ani jednej świecy — tam nie ma czego wznawiać.

Plan przeżywa wdrożenie, a zapisana w nim notatka o błędzie — nie: pochodzi z kodu, który
mógł uznawać za awarię coś, co dziś jest zwykłą dziurą. Dlatego plan nosi **numer wersji
zasad** (`archive_rules` w `/api/diagnostics`). Po jego podbiciu nieudane instrumenty ze
starszego planu dostają drugą szansę same z siebie — wystarczy wejść na stronę. Przerwanych
planów to nie dotyczy: przerwanie jest decyzją użytkownika i wdrożenie jej nie cofa.

Plan „w trakcie", którego nikt nie posuwa od kwadransa, jest uznawany za porzucony —
zamknięta w połowie karta nie blokuje kolejnego pobierania.

Dopiero domknięty instrument trafia do biblioteki jako jeden gotowy zbiór; niedokończone
kawałki są tylko rusztowaniem i znikają po sklejeniu.

#### To samo z wiersza poleceń

Do pracy lokalnej jest też skrypt — nie wymaga otwartej przeglądarki i pobiera jednym ciągiem:

```bash
python3 tools/pobierz_archiwum.py --sprawdz      # najpierw oszacowanie, nic nie pobiera
python3 tools/pobierz_archiwum.py                # 10 lat, wszystkie instrumenty
python3 tools/pobierz_archiwum.py --lata 3 --instrumenty GBPUSD EURUSD
python3 tools/pobierz_archiwum.py --interwal 5 --cena mid
```

Skala jest spora: **10 lat × 8 instrumentów to pół miliona plików godzinowych i ~130 MB
gotowych CSV**, czyli od dwudziestu minut do godziny pobierania. Tryb `--sprawdz` poda
szacunek dla Twoich ustawień, zanim cokolwiek ruszy.

Pobieranie da się przerwać `Ctrl+C`. Ściągnięte godziny zostają w pamięci podręcznej, więc
ponowne uruchomienie dokończy resztę — sprawdzone: powtórka nie sięga po ani jeden plik
godzinowy z sieci.

Skrypt pobiera jednym ciągiem, bez dzielenia na kroki, więc **uruchamiaj go lokalnie** — na
wdrożeniu bezserwerowym nie zmieściłby się w limicie czasu żądania. Tam użyj panelu opisanego
wyżej; robi dokładnie to samo, tylko krokami. Gotowe pliki CSV z katalogu `data/datasets/`
możesz też po prostu wgrać na wdrożenie zwykłym „Wgraj plik".

#### Trwałość biblioteki

Lokalnie pliki leżą w katalogu `data/datasets/` i przeżywają restart — nie ma tu o czym myśleć.

Przy wdrożeniu bezserwerowym jedynym zapisywalnym miejscem jest katalog tymczasowy: ulotny
i lokalny dla instancji. Bez dodatkowego kroku biblioteka jest tam wygodą w obrębie sesji,
a nie archiwum. Żeby zapis przetrwał uśpienie i kolejne wdrożenia, podepnij magazyn obiektów
**Vercel Blob** — zajmuje to dwie minuty i jest opisane krok po kroku w [DEPLOY.md](DEPLOY.md).
Aplikacja szuka zmiennej `BLOB_READ_WRITE_TOKEN`, którą Vercel dodaje sam przy podpięciu magazynu.

Przełączenie niczego nie gubi: odczyt sprawdza najpierw magazyn, potem dysk instancji, więc
zbiory zapisane wcześniej są nadal widoczne. W drugą stronę tak samo — gdy magazyn przestaje
odpowiadać, aplikacja pracuje dalej na dysku i mówi o tym w panelu, zamiast obiecywać trwałość,
której akurat nie ma. Przycisk **„Sprawdź magazyn"** wykonuje pełny cykl zapis → odczyt →
porównanie → usunięcie i rozstrzyga jednym kliknięciem, co się faktycznie dzieje na Twoim
wdrożeniu.

> Rozmowa z Vercel Blob idzie po jego API REST i **nie została sprawdzona na żywo** — w środowisku,
> w którym projekt powstawał, ruch wychodzący był zablokowany. Testy pokrywają naszą stronę
> umowy na atrapie magazynu; drugą stronę rozstrzyga dopiero „Sprawdź magazyn" na wdrożeniu.
> Dlatego każda operacja ma odwrót na dysk — awaria magazynu nie zatrzymuje aplikacji.

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
Yahoo oddaje dla 15 minut najwyżej około 60 dni, niezależnie od tego, o ile poprosisz.

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
Dukascopy nie ma tego ograniczenia — sięga 2003 roku i to jego używaj do wieloletnich testów.

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
| Z czego czytać kierunek świecy | Trzy sposoby. **Korpus** (domyślnie) — otwarcie kontra zamknięcie, knoty pomijane. **Wychylenia** — które sięgnęło dalej od otwarcia: górne (szczyt − otwarcie) czy dolne (otwarcie − dołek); zamknięcie nie ma znaczenia. **Zakres** — zamknięcie kontra środek między szczytem a dołkiem. Dotyczy tylko strategii 1. |
| Kierunek pozycji | `Podążaj za świecą` = pierwotna logika. Można ją odwrócić albo ograniczyć do samych longów lub shortów. |
| Gdy świeca bez zmiany | Co zrobić, gdy otwarcie równa się zamknięciu (doji). Tylko strategia 1. |

### 2b · Ustawienia strategii „wybicie zakresu"

Widoczne po przełączeniu na drugą strategię:

| Pole | Znaczenie |
|---|---|
| Do kiedy czekać na wybicie | **Do końca dnia** (domyślnie), **do określonej godziny** (np. 17:00, koniec sesji londyńskiej) albo **przez N godzin** od zamknięcia świecy. Brak wybicia w oknie = dzień bez transakcji. |
| Co uznajemy za przebicie | **Dotknięcie poziomu** — wystarczy, że cena sięgnie granicy; wejście po cenie tego poziomu, tak jak zadziałałoby zlecenie stop. **Zamknięcie poza zakresem** — świeca musi się zamknąć poza granicą; odfiltrowuje przekłucia knotem, ale wchodzi później i dalej, więc ryzyko na transakcję rośnie. |
| Bufor wybicia | Ile pipsów cena musi wyjść poza zakres, żeby wybicie się liczyło. Filtruje płytkie przekłucia. 0 = bez filtra. |
| Gdy jedna świeca przebija obie granice | Z samego OHLC nie wynika, który poziom padł pierwszy. Domyślnie zakładamy, że **bliższy otwarciu** tej świecy (cena rusza od otwarcia). Wariant **„ta strona, w którą cena wyszła dalej"** nie modeluje kolejności, tylko odpowiada na pytanie, którędy rynek faktycznie wyszedł z zakresu — zwykle zgadza się z tym, co widać na wykresie. Można też wymusić stronę albo pominąć taki dzień jako nierozstrzygalny. |

> **Nie zgadza się z Twoim wykresem? Kliknij w wiersz tabeli.** Rozwinie się podgląd świec
> z tego dnia — dokładnie tych, na których liczył silnik — z podświetlonym oknem świecy
> sygnałowej i godziną podaną w dwóch kolumnach: w ustawionej strefie i w UTC. Jeśli Twój
> wykres zgadza się z kolumną UTC, a nie z pierwszą, przyczyną jest strefa czasowa w sekcji 1,
> a nie logika strategii. Dane z Dukascopy są w UTC, a domyślna strefa aplikacji to
> Europe/London — latem różnica wynosi godzinę.

> **Otwarcie na granicy nie jest jej przebiciem.** Świeca sygnałowa często zamyka się na swoim
> skraju, a następna stamtąd startuje — dystans od otwarcia do tej granicy wynosi wtedy zero.
> Reguła „bliższy otwarciu" wskazywałaby ją zawsze, choćby cena natychmiast poszła w drugą
> stronę, i dawała pozycję odwrotną do widocznego ruchu. W takim układzie o kierunku decyduje
> więc zamknięcie świecy. Otwarcie **poza** zakresem (luka) to co innego: pierwsza cena świecy
> leży już po tamtej stronie i to rozstrzyga wybicie, niezależnie od tego, co działo się dalej.
| Co cena ma przebić | **Pełne wychylenia** (domyślnie) — szczyt i dołek świecy, razem z knotami. **Krańce korpusu** — otwarcie i zamknięcie; leżą bliżej, więc wybicia padają częściej i wcześniej. |
| Gdzie postawić stop loss | Niezależnie od granicy wejścia. **Tam, gdzie druga granica** (domyślnie) — jak dotąd. **Za pełnym wychyleniem** albo **na krańcu korpusu** — pozwala wejść wcześnie na ciasnym korpusie, a stop trzymać dopiero za knotem, albo odwrotnie: wejść na pełnym wybiciu i trzymać ciasny stop przy korpusie. |
| Powtórki w ciągu dnia | **Jedna transakcja dziennie** (domyślnie), **dopuść wybicie w drugą stronę** po zamknięciu pierwszej pozycji, albo **każde kolejne wybicie** aż do limitu dziennego. Kolejna próba nigdy nie startuje przed zamknięciem poprzedniej. |

Przy tej strategii metoda stop lossa **„zakres świecy"** oznacza przeciwną granicę zakresu —
przy wybiciu górą stop ląduje na dołku świecy, przy wybiciu dołem na jej szczycie. Pozostałe
metody (stałe pipsy, procent ceny) działają tak samo jak w strategii 1.

W trybie **odwróconym** (gra przeciw wybiciu, czyli na fałszywe wybicie) dystans ryzyka jest
mierzony tak samo — do przeciwnej granicy zakresu — ale stop ląduje po drugiej stronie wejścia,
czyli powyżej wybicia górą. Bez tego dystans wychodziłby zerowy, bo wejście leży dokładnie
na granicy zakresu.

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
- **Strategia wybicia jest na to szczególnie wrażliwa.** Świeca, na której następuje wybicie,
  bywa na tyle szeroka, że sięga też stop lossa po przeciwnej stronie zakresu — a z OHLC nie
  wynika, czy dołek wypadł przed wybiciem, czy po nim. Obowiązuje wtedy ustawienie
  „gdy jedna świeca dotyka i SL, i TP", domyślnie pesymistyczne. To samo dotyczy świecy
  przebijającej obie granice naraz. Dane 1-minutowe z Dukascopy rozstrzygają oba przypadki
  znacznie dokładniej i przy tej strategii warto po nie sięgnąć.

  **Takie wiersze są oznaczone.** W kolumnie z wynikiem pojawia się przy nich znak **?**
  z wyjaśnieniem — to pozycje, o których losie zdecydowało ustawienie, a nie dane. Dzięki temu
  widać od razu, ile wyników jest niepewnych, zamiast zgadywać, czemu „wygrana" pozycja
  została zamknięta stopem. W eksporcie CSV odpowiada temu kolumna `wynik_niepewny`.
  Na danych demo wychodzi ich około 8 % — im drobniejsze świece, tym mniej.
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
  fetch.py        pobieranie z Yahoo Finance oknami wstecz
  dukascopy.py    pobieranie archiwum tickowego i składanie go w świece
  runtime.py      rozpoznanie środowiska (lokalne kontra bezserwerowe)
  main.py         serwer HTTP i API
api/index.py      punkt wejścia funkcji bezserwerowej na Vercelu
web/              frontend (HTML, CSS, czysty JavaScript — bez zależności)
tools/            generator danych demo, masowe pobranie archiwum
tests/            testy jednostkowe
vercel.json       konfiguracja wdrożenia
```

## Testy

```bash
python3 -m pytest tests/ -q
```

Testy sprawdzają parser CSV (warianty formatu czasu, separatory, odrzucanie złych wierszy),
pobieranie z sieci oknami wstecz (sklejanie okien, granica archiwum, budżet czasu),
obie strategie, wszystkie warianty wyjścia z pozycji, trzy metody stop lossa, trzy tryby
wielkości pozycji, wszystkie cztery tryby zarządzania pozycją, poprawność stref czasowych,
zatrzymanie backtestu przy wyzerowaniu kapitału oraz dekoder archiwum Dukascopy.

Dla strategii wybicia dochodzą testy okien czasowych, obu wyzwalaczy, bufora, wejścia przy luce
otwarcia, czterech wariantów rozstrzygania wybicia obustronnego i trzech trybów powtórek.

## API

Frontend korzysta z tych samych endpointów, więc można je wołać skryptem:

| Endpoint | Opis |
|---|---|
| `GET /api/options` | Listy opcji i wartości domyślne. |
| `POST /api/upload` | Wgranie pliku CSV (multipart). Zwraca `dataset_id`. |
| `GET /api/sample` | Wczytanie danych demo. |
| `POST /api/fetch` | `{"interval": "15m", "days": 60, "symbol": "GBPUSD=X"}` → pobiera oknami wstecz, aż uzbiera okres albo wyczerpie archiwum dostawcy. |
| `POST /api/dukascopy/start` | Start pobierania z Dukascopy w tle. Zwraca `job_id`. |
| `GET /api/dukascopy/status/{job_id}` | Postęp pobierania, a po zakończeniu gotowy zbiór danych. |
| `POST /api/dukascopy/cancel/{job_id}` | Przerwanie pobierania. |
| `POST /api/backtest` | `{"dataset_id": "...", "config": {...}}` → pełne wyniki. Pole `strategy` wybiera `candle_direction` albo `range_breakout`. |
| `POST /api/compare` | `{"dataset_id": "...", "configs": {"candle_direction": {...}, "range_breakout": {...}}}` → oba komplety wyników naraz. |
| `GET /api/datasets` | Lista zapisanych zbiorów razem z opisem i informacją, czy zapis jest trwały. |
| `POST /api/datasets/{id}/open` | Wczytuje zapisany zbiór ponownie — bez sieci i bez wysyłania pliku. |
| `PATCH /api/datasets/{id}` | Zmiana nazwy zbioru. |
| `DELETE /api/datasets/{id}` | Usunięcie zbioru z biblioteki. |
| `GET /api/datasets/{id}/csv` | Pobranie zapisanego zbioru jako pliku CSV. |
| `POST /api/dukascopy/chunk` | Pobiera tyle dni, ile zmieści się w limicie czasu, i zwraca surowy CSV razem z ostatnim domkniętym dniem. Klient wznawia od następnego. |
| `GET /api/dukascopy/probe` | Pobiera jeden testowy plik godzinowy i opisuje wynik — diagnostyka na wypadek, gdy pobieranie nie rusza. |
| `GET /api/storage/probe` | Pełny cykl zapis → odczyt → porównanie → usunięcie. Rozstrzyga, czy biblioteka przeżyje uśpienie instancji. |
| `GET /api/diagnostics` | Czy wdrożenie ma wszystko, czego potrzebuje: pliki frontu, dane demo, magazyn, wersję Pythona. |
| `GET /api/datasets/{id}/candles?date=…` | Surowe świece z jednego dnia, z godziną w ustawionej strefie i w UTC — do porównania wiersza tabeli z własnym wykresem. |
| `GET /api/archive/estimate` | Ile plików, danych i czasu zajmie masowe pobranie — bez pobierania czegokolwiek. |
| `POST /api/archive/start` | `{"instruments": ["GBPUSD"], "years": 10, "interval_minutes": 15, "price": "bid"}` → zakłada plan pobierania. |
| `POST /api/archive/step` | Wykonuje tyle pracy, ile mieści się w limicie czasu, i oddaje postęp. Wołane w pętli aż plan przestanie być `running`. |
| `GET /api/archive/status` | Stan planu — także po zamknięciu przeglądarki albo z innej instancji. |
| `POST /api/archive/retry` | Wznawia instrumenty, które skończyły błędem, od ich kursorów; udanych nie tyka. |
| `POST /api/archive/cancel` | Przerywa pobieranie; instrumenty domknięte wcześniej zostają w bibliotece. |
