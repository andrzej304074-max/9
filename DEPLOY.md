# Wdrożenie na Vercel

Aplikacja jest przygotowana do wdrożenia bezserwerowego. Frontend i API działają z jednej
funkcji Pythona, a wszystko, co wymagało zapisu na dysk albo pamięci między żądaniami,
zostało do tego środowiska dostosowane.

---

## Zanim zaczniesz — co się zmienia w chmurze

Vercel uruchamia kod jako **funkcję bezserwerową**. To nie jest serwer, który stoi i pamięta,
co robił minutę temu. Wynikają z tego trzy realne różnice, o których warto wiedzieć, zanim
klikniesz „Deploy":

| | Lokalnie | Na Vercelu |
|---|---|---|
| **Pobieranie z Dukascopy** | Bez limitu, jedno zadanie w tle, pasek postępu co plik | Dowolny zakres, pobierany częściami dobieranymi automatycznie do limitu czasu |
| **Wgrywanie plików CSV** | Do 64 MB | Do 4 MB **po kompresji** (≈ 20 MB CSV, czyli kilkanaście lat świec 15-minutowych) |
| **Pamięć między żądaniami** | Trwała, dopóki serwer działa | Ulotna — aplikacja sama wysyła dane ponownie, gdy trafi na świeżą instancję |
| **Biblioteka zapisanych zbiorów** | Katalog `data/`, przeżywa restart | Ulotna, dopóki nie podepniesz magazynu obiektów — [instrukcja niżej](#trwała-biblioteka-danych-vercel-blob) |

Pierwsze trzy różnice nie wymagają od Ciebie niczego w trakcie pracy — aplikacja radzi sobie
z nimi sama. Możesz spokojnie wybrać kilka lat historii: serwer pobiera tyle, ile zdąży
w jednym żądaniu, raportuje, do którego dnia doszedł, a przeglądarka wznawia od następnego —
aż do końca zakresu. Skompletowaną całość wysyła na serwer jako jeden plik. Potrwa to
proporcjonalnie dłużej niż lokalnie, a przerwać można między częściami.

Czwarta wymaga jednej decyzji: bez podpiętego magazynu raz pobrane dane trzeba pobrać
ponownie po każdym uśpieniu serwera. Podpięcie zajmuje dwie minuty i jest opisane niżej.

---

## Wdrożenie przez stronę Vercela (najprościej)

1. **Wypchnij kod na GitHub.** Gałąź `claude/gbp-usd-backtester-9jria8` już tam jest; możesz
   też scalić ją do `main`.

2. Wejdź na [vercel.com/new](https://vercel.com/new) i zaloguj się kontem GitHub.

3. **Zaimportuj repozytorium** — Vercel pokaże listę Twoich repozytoriów, wybierz to z backtesterem.

4. Na ekranie konfiguracji **nie zmieniaj nic**:
   - *Framework Preset* — `Other`
   - *Build Command*, *Output Directory*, *Install Command* — zostaw puste
   - Plik `vercel.json` w repozytorium ustawia resztę samodzielnie

5. Kliknij **Deploy** i poczekaj około minuty.

Dostaniesz adres w rodzaju `https://twoj-projekt.vercel.app`. Otwórz go, kliknij **Dane demo**
i sprawdź, czy backtest się liczy. Gdyby zamiast aplikacji pojawił się surowy tekst, zajrzyj
do sekcji [Rozwiązywanie problemów](#rozwiązywanie-problemów) — pierwszy punkt opisuje właśnie
ten przypadek.

---

## Wdrożenie z terminala

```bash
npm i -g vercel      # jednorazowo
cd /ścieżka/do/projektu

vercel login
vercel               # wdrożenie podglądowe (preview)
vercel --prod        # wdrożenie produkcyjne
```

Przy pierwszym uruchomieniu Vercel zapyta o kilka rzeczy — na wszystkie odpowiedz domyślnie
(*Enter*), a na pytanie o katalog projektu podaj `./`.

---

## Co jest w repozytorium i po co

| Plik | Rola |
|---|---|
| `api/index.py` | Punkt wejścia funkcji. Vercel szuka w nim zmiennej `app` i traktuje ją jako aplikację ASGI. |
| `vercel.json` | `outputDirectory` wskazuje `web/` jako statyczną stronę, a `rewrites` kieruje do funkcji wyłącznie `/api/*`. `includeFiles` dokłada do paczki funkcji pliki, których runtime nie wyśledzi po importach. |

### Dlaczego strona idzie osobno od API

Front (`web/`) jest serwowany jako **statyka z CDN-u**, a funkcja Pythona obsługuje tylko
ścieżki `/api/*`. To nie jest kosmetyka: gdyby wszystko szło przez funkcję, dowolny jej
problem — brakujący plik w paczce, wyjątek przy starcie — kończyłby się nie stroną z błędem,
tylko surowym tekstem zamiast aplikacji. Przy tym podziale strona wstaje niezależnie od
Pythona, a ewentualny problem z API widać jako komunikat w interfejsie.

### Dlaczego reguła przekierowania ma na końcu `?__sciezka=$1`

Bo `rewrites` na Vercelu **podmienia ścieżkę** żądania na docelową. Reguła
`/api/(.*)` → `/api/index` sprawia, że funkcja dostaje `/api/index` niezależnie od tego,
o co pytał front — a takiej trasy aplikacja nie ma, więc każde wywołanie API kończy się 404,
choć serwer działa bez zarzutu.

Reguła dokleja więc pierwotną ścieżkę jako parametr, a aplikacja wstawia ją z powrotem,
zanim zadziała trasowanie. Warunek jest wąski: dzieje się to wyłącznie wtedy, gdy ścieżką
jest sam punkt wejścia, więc lokalnie i przy zachowanej ścieżce nic się nie zmienia.
Gdyby podstawienie `$1` kiedyś przestało działać, API odpowie komunikatem wskazującym
wprost tę regułę, zamiast milczącym 404.
| `requirements.txt` | Zależności instalowane w chmurze — tylko `fastapi` i `python-multipart`. |
| `requirements-dev.txt` | To samo plus `uvicorn` i `pytest`, do pracy lokalnej. |
| `.vercelignore` | Trzyma testy, narzędzia i pamięć podręczną poza wdrożeniem. |
| `app/runtime.py` | Rozpoznaje środowisko i w jednym miejscu zbiera wynikające z niego decyzje. |

---

## Trwała biblioteka danych (Vercel Blob)

Aplikacja zapisuje każdy zbiór, który przez nią przeszedł — wgrany plik, pobranie z Dukascopy,
pobranie z Yahoo — żeby dało się do niego wrócić bez powtarzania pobierania. Widać je w panelu
**Zapisane dane**.

Na Vercelu jedynym miejscem z prawem zapisu jest katalog `/tmp`, a ten znika razem z instancją.
Bez dodatkowego kroku biblioteka jest więc wygodą w obrębie sesji, a nie archiwum: po kilkunastu
minutach bezczynności lista bywa pusta. Żeby zapis przetrwał uśpienie **i kolejne wdrożenia**,
podepnij magazyn obiektów Vercel Blob.

### Krok po kroku

1. Wejdź w panelu Vercela w swój projekt → zakładka **Storage**.
2. Kliknij **Create Database** (albo **Create Store**) i wybierz **Blob**.
3. Nadaj nazwę — dowolną, np. `backtester-dane` — i potwierdź.
4. Na ekranie po utworzeniu wybierz **Connect Project** i wskaż projekt z backtesterem.
   Zaznacz wszystkie środowiska (*Production*, *Preview*, *Development*).
5. Vercel sam doda do projektu zmienną środowiskową `BLOB_READ_WRITE_TOKEN`. Nie musisz jej
   nigdzie kopiować ani wpisywać — aplikacja szuka dokładnie tej nazwy.
6. **Wdróż projekt ponownie.** Zmienne środowiskowe wchodzą w życie dopiero przy nowym
   wdrożeniu: zakładka **Deployments** → menu `…` przy ostatnim wdrożeniu → **Redeploy**.

### Sprawdzenie, czy zadziałało

Otwórz aplikację i w panelu **Zapisane dane** kliknij **Sprawdź magazyn**. Serwer wykona pełny
cykl — zapisze plik próbny, odczyta go, porówna treść i usunie — po czym powie wprost, co wyszło:

* **„Zapis jest trwały"** — gotowe, biblioteka przeżyje uśpienie i kolejne wdrożenia.
* **„…trafiają na dysk instancji"** — magazyn nie jest podpięty (brakuje zmiennej) albo
  wdrożenie jest jeszcze sprzed jej dodania. Powtórz krok 6.
* **„Token jest, ale…"** — zmienna jest, lecz magazyn nie odpowiada. Sprawdź w zakładce
  **Storage**, czy magazyn nadal jest połączony z tym projektem.

To samo dostaniesz pod adresem `https://twoj-projekt.vercel.app/api/storage/probe`, gdyby
wygodniej było zajrzeć tam wprost.

### Co warto wiedzieć

* **Nic nie ginie przy przełączeniu.** Zbiory zapisane przed podpięciem magazynu są nadal
  widoczne — odczyt sprawdza najpierw magazyn, potem dysk instancji. W drugą stronę tak samo:
  gdyby magazyn przestał odpowiadać, aplikacja pracuje dalej na dysku i mówi o tym w panelu,
  zamiast obiecywać trwałość, której akurat nie ma.
* **Koszt.** Darmowy pułap Vercel Blob to obecnie kilka GB przestrzeni; dziesięć lat świec
  15-minutowych dla jednego instrumentu to kilkanaście MB, więc miejsca starcza na komplet
  instrumentów z dużym zapasem. Aktualne limity sprawdź w cenniku Vercela — potrafią się zmieniać.
* **Ten sam magazyn można podpiąć do kilku projektów** (np. produkcyjnego i podglądowego).
  Wszystko leży pod prefiksem `backtester/`, więc nie miesza się z innymi zastosowaniami magazynu.
* **Uczciwe zastrzeżenie.** Kod rozmawia z Vercel Blob po jego API REST, ale w środowisku,
  w którym powstawał, ruch wychodzący jest zablokowany — nie dało się tego sprawdzić na żywo
  ani razu. Dlatego każda operacja ma odwrót na dysk (awaria magazynu nie zatrzymuje aplikacji),
  a przycisk **Sprawdź magazyn** istnieje właśnie po to, żebyś rozstrzygnął to jednym
  kliknięciem na swoim wdrożeniu, zamiast wierzyć na słowo.

### Zamiast tego: własny komputer

Jeśli wolisz uniknąć magazynu, uruchom aplikację lokalnie (`uvicorn app.main:app`). Biblioteka
leży wtedy w katalogu `data/datasets/`, jest trwała z definicji i nie ma żadnych limitów czasu
ani rozmiaru żądania. Skryptem `tools/pobierz_archiwum.py` ściągniesz do niej dziesięć lat
historii wszystkich instrumentów naraz, a gotowe pliki CSV wgrasz potem na wdrożenie.

---

## Pobranie całego archiwum na wdrożeniu

W panelu **Zapisane dane** jest sekcja **„Pobierz całe archiwum Dukascopy"**: zaznaczasz
instrumenty, podajesz liczbę lat, długość świecy i cenę. Przed startem widać oszacowanie —
ile plików, ile danych i orientacyjnie ile czasu.

Dziesięć lat dla kompletu instrumentów to pół miliona plików godzinowych. Nie mieści się to
w żadnym pojedynczym żądaniu, więc praca dzieli się na krótkie kroki, każdy w granicach
`maxDuration`. Plan pobierania leży w magazynie razem z biblioteką, dzięki czemu kolejny krok
może trafić na dowolną instancję i podejmuje pracę od zapisanego kursora.

W praktyce:

* **Zostaw kartę otwartą.** To przeglądarka wywołuje kolejne kroki. Zamknięcie jej wstrzymuje
  pobieranie, nie kasuje — po powrocie aplikacja sama je podejmuje.
* **Podepnij najpierw magazyn.** Bez niego pobrane archiwum zniknie razem z instancją i cała
  praca pójdzie na marne.
* **Plan Hobby wystarczy**, tylko potrwa dłużej: przy limicie 60 s krok bierze około 40 s pracy,
  więc kroków jest po prostu więcej. Na planie Pro (`maxDuration` 300) idzie to pięciokrotnie
  szybciej — patrz sekcja o planie Pro niżej.
* **Zacznij od jednego instrumentu i jednego roku.** Zobaczysz realne tempo swojego wdrożenia,
  zanim zamówisz komplet.

Alternatywa bez czekania: uruchom `tools/pobierz_archiwum.py` u siebie na komputerze
i wgraj gotowe pliki CSV przez „Wgraj plik". Przy podpiętym magazynie zostaną na stałe.

---

## Ustawienia opcjonalne

Wszystkie mają rozsądne wartości domyślne — sięgaj po nie tylko wtedy, gdy coś rzeczywiście
wymaga zmiany. Ustawia się je w panelu Vercela: **Settings → Environment Variables**.

| Zmienna | Domyślnie | Kiedy zmieniać |
|---|---|---|
| `BACKTESTER_MAX_SECONDS` | `60` | Gdy masz plan Pro i podniesiesz `maxDuration` w `vercel.json` — pobieranie zacznie brać większe porcje na jedno żądanie. |
| `BACKTESTER_MAX_UPLOAD` | `4194304` | Gdyby platforma podniosła limit ciała żądania. |
| `BACKTESTER_STATE_DIR` | `/tmp/backtester` | Praktycznie nigdy. |
| `BLOB_READ_WRITE_TOKEN` | brak | Ustawia go sam Vercel przy podpięciu magazynu Blob — [patrz wyżej](#trwała-biblioteka-danych-vercel-blob). Ręcznie tylko wtedy, gdy chcesz użyć magazynu z innego projektu. |

### Plan Pro — szybsze pobieranie

Na planie Pro możesz wydłużyć limit czasu. W `vercel.json` zmień `maxDuration` na `300`,
a w zmiennych środowiskowych ustaw `BACKTESTER_MAX_SECONDS=300`. Pobieranie z Dukascopy
będzie wtedy brało pięciokrotnie większe porcje na jedno żądanie, czyli ten sam zakres
skompletuje się w mniejszej liczbie części. Na planie Hobby też się skompletuje — po prostu
w większej liczbie kroków.

---

## Sprawdzenie po wdrożeniu

Otwórz adres aplikacji i przejdź po kolei:

1. **Dane demo** → powinno wczytać 14 112 świec i policzyć backtest.
2. **Przełącznik strategii** → obie liczą się na tym samym zbiorze.
3. **Porównaj obie strategie** → dwie kolumny i dwie krzywe na wykresie.
4. **Wgraj własny CSV** → plik jest pakowany w przeglądarce, więc duże eksporty też przejdą.
5. **Zapisane dane → Sprawdź magazyn** → mówi wprost, czy biblioteka przeżyje uśpienie
   serwera. Jeżeli nie, podepnij magazyn według [instrukcji wyżej](#trwała-biblioteka-danych-vercel-blob).

Jeżeli po kilku minutach bezczynności coś przestanie odpowiadać na pierwsze kliknięcie —
to zimny start funkcji. Aplikacja sama wyśle dane ponownie; zobaczysz jedynie chwilę dłuższe
oczekiwanie, bez komunikatu o błędzie.

---

## Rozwiązywanie problemów

**Zamiast aplikacji widzę surowy tekst — `{"detail":"Not Found"}` albo stronę błędu Vercela.**

Zacznij od rozstrzygnięcia, **czy w ogóle działa nowy kod**. Otwórz
`https://twoj-projekt.vercel.app/api/diagnostics`:

* **Widzisz JSON z polami `web_dir_present`, `frontend_ready`** → nowy kod jest wdrożony.
  Te pola mówią, czego brakuje.
* **Widzisz `404: NOT_FOUND` albo stronę błędu Vercela** → wdrożony jest **stary kod**.
  To najczęstsza przyczyna „poprawka nic nie zmieniła" i osobny problem, opisany niżej.

**Uwaga na przycisk „Redeploy": on wdraża ten sam commit co poprzednio.** Do zmian w kodzie
służy nowe wdrożenie z najnowszego commitu. Sprawdź w zakładce **Deployments**, czy najnowsze
wdrożenie ma opis Twojego ostatniego commitu i datę po jego wypchnięciu. Jeżeli nie:

1. **Settings → Git** — czy projekt jest w ogóle połączony z repozytorium. Bez tego wypchnięcie
   na GitHuba niczego nie uruchamia i każde wdrożenie trzeba robić ręcznie (`vercel --prod`).
2. **Settings → Git → Production Branch** — czy zgadza się z gałęzią, na której jest kod.
   Domyślnie Vercel bierze gałąź główną repozytorium.
3. Gdy projekt jest połączony, a wdrożenie nie ruszyło — zrób **Redeploy na najnowszym
   wdrożeniu** i odznacz *Use existing Build Cache*.

**Strona pokazuje 404 zamiast aplikacji.** Sprawdź, czy `vercel.json` trafił do repozytorium
i czy w ustawieniach projektu *Output Directory* jest puste.

**`ModuleNotFoundError: No module named 'app'`.** Katalog `app/` nie został wdrożony — upewnij
się, że nie dopisałeś go do `.vercelignore` i że jest śledzony przez gita (`git ls-files app/`).

**Klikam „Pobierz z Dukascopy" i nic się nie dzieje.** Kliknij obok **„Sprawdź połączenie"** —
serwer spróbuje pobrać jeden testowy plik i powie wprost, co się stało: czy archiwum
odpowiada, ile to trwało, czy zwróciło błąd, czy ruch jest blokowany. To rozstrzyga
w sekundę, czy problem jest po stronie sieci, czy aplikacji.

**Pobieranie z Dukascopy przerywa się w połowie.** Pojedyncze nieudane godziny nie zatrzymują
już pobierania — są pomijane i zliczane, a po zakończeniu dostajesz informację, ile ich było.
Powtórzenie pobrania uzupełni luki, bo reszta jest już w pamięci podręcznej. Komunikat
„nie udało się pobrać ani jednego pliku" oznacza natomiast realny problem z łączem albo
z dostępem do archiwum.

**Komunikat, że scalony plik przekracza limit.** Zakres pobrał się w całości, ale komplet
danych nie mieści się w limicie żądania. Wybierz rzadszy interwał (np. 15 minut zamiast
1 minuty — to najskuteczniejsze) albo krótszy okres. Świece 1-minutowe są objętościowo
mniej więcej piętnaście razy cięższe od 15-minutowych.

**Wgranie pliku kończy się błędem „za duży".** Limit dotyczy rozmiaru **po kompresji**.
Typowy CSV kurczy się około pięciokrotnie, więc 4 MB odpowiada mniej więcej 20 MB pliku
źródłowego. Większe zbiory podziel na okresy albo uruchom aplikację lokalnie.

**Biblioteka „Zapisane dane" jest pusta, choć wczoraj coś w niej było.** Instancja została
uśpiona i zabrała ze sobą katalog `/tmp`. Kliknij **Sprawdź magazyn** — jeżeli mówi, że zapis
trafia na dysk instancji, podepnij magazyn Blob według
[instrukcji wyżej](#trwała-biblioteka-danych-vercel-blob).

**Magazyn Blob jest podpięty, a mimo to biblioteka znika.** Zmienne środowiskowe wchodzą
w życie dopiero przy nowym wdrożeniu — zrób **Redeploy**. Jeżeli po nim **Sprawdź magazyn**
nadal nie mówi „zapis jest trwały", zajrzyj w zakładkę **Storage**, czy magazyn jest połączony
akurat z tym projektem i z tym środowiskiem (produkcyjne i podglądowe mają osobne zmienne).

**Logi.** Panel Vercela → zakładka **Logs** pokazuje wywołania funkcji razem z komunikatami
błędów po stronie Pythona.
