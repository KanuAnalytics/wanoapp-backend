"""Cities people can pick as their Home Ground: Africa and the diaspora.
(name, country code). Ids are made from the name. Admins add more from
the admin page (home_ground.add_city), so this list never needs an app update."""

COUNTRY_NAMES = {
    # Africa
    "dz": "Algeria", "ao": "Angola", "bj": "Benin", "bw": "Botswana", "bf": "Burkina Faso", "bi": "Burundi",
    "cv": "Cabo Verde", "cm": "Cameroon", "cf": "Central African Republic", "td": "Chad", "km": "Comoros",
    "cg": "Congo", "cd": "DR Congo", "ci": "Côte d'Ivoire", "dj": "Djibouti", "eg": "Egypt", "gq": "Equatorial Guinea",
    "er": "Eritrea", "sz": "Eswatini", "et": "Ethiopia", "ga": "Gabon", "gm": "Gambia", "gh": "Ghana", "gn": "Guinea",
    "gw": "Guinea-Bissau", "ke": "Kenya", "ls": "Lesotho", "lr": "Liberia", "ly": "Libya", "mg": "Madagascar",
    "mw": "Malawi", "ml": "Mali", "mr": "Mauritania", "mu": "Mauritius", "ma": "Morocco", "mz": "Mozambique",
    "na": "Namibia", "ne": "Niger", "ng": "Nigeria", "rw": "Rwanda", "st": "São Tomé and Príncipe", "sn": "Senegal",
    "sc": "Seychelles", "sl": "Sierra Leone", "so": "Somalia", "za": "South Africa", "ss": "South Sudan",
    "sd": "Sudan", "tz": "Tanzania", "tg": "Togo", "tn": "Tunisia", "ug": "Uganda", "zm": "Zambia", "zw": "Zimbabwe",
    # Diaspora
    "us": "United States", "ca": "Canada", "gb": "United Kingdom", "ie": "Ireland", "fr": "France", "be": "Belgium",
    "nl": "Netherlands", "de": "Germany", "it": "Italy", "es": "Spain", "pt": "Portugal", "ch": "Switzerland",
    "se": "Sweden", "no": "Norway", "dk": "Denmark", "fi": "Finland", "at": "Austria", "ae": "United Arab Emirates",
    "sa": "Saudi Arabia", "qa": "Qatar", "kw": "Kuwait", "om": "Oman", "il": "Israel", "tr": "Türkiye", "cn": "China",
    "in": "India", "jp": "Japan", "au": "Australia", "nz": "New Zealand", "br": "Brazil", "co": "Colombia",
    "ve": "Venezuela", "ec": "Ecuador", "pe": "Peru", "mx": "Mexico", "pa": "Panama", "cr": "Costa Rica",
    "hn": "Honduras", "bz": "Belize", "gt": "Guatemala", "ni": "Nicaragua", "cu": "Cuba", "do": "Dominican Republic",
    "ht": "Haiti", "jm": "Jamaica", "tt": "Trinidad and Tobago", "bb": "Barbados", "bs": "Bahamas", "pr": "Puerto Rico",
    "vc": "Saint Vincent and the Grenadines", "lc": "Saint Lucia", "gd": "Grenada", "ag": "Antigua and Barbuda",
    "kn": "Saint Kitts and Nevis", "dm": "Dominica", "gy": "Guyana", "sr": "Suriname", "ar": "Argentina",
    "uy": "Uruguay", "cl": "Chile",
}

CITY_LIST = [
    # Uganda
    ("Kampala", "ug"), ("Gulu", "ug"), ("Mbarara", "ug"), ("Jinja", "ug"), ("Entebbe", "ug"), ("Mbale", "ug"),
    ("Lira", "ug"), ("Arua", "ug"), ("Masaka", "ug"), ("Fort Portal", "ug"), ("Soroti", "ug"), ("Mukono", "ug"),
    ("Wakiso", "ug"), ("Hoima", "ug"), ("Kabale", "ug"), ("Kasese", "ug"), ("Tororo", "ug"), ("Iganga", "ug"),
    ("Masindi", "ug"), ("Busia", "ug"), ("Kitgum", "ug"), ("Moroto", "ug"), ("Kotido", "ug"), ("Mityana", "ug"),
    # Kenya
    ("Nairobi", "ke"), ("Mombasa", "ke"), ("Kisumu", "ke"), ("Nakuru", "ke"), ("Eldoret", "ke"), ("Thika", "ke"),
    ("Machakos", "ke"), ("Nyeri", "ke"), ("Malindi", "ke"), ("Kakamega", "ke"), ("Kisii", "ke"), ("Meru", "ke"),
    ("Kitale", "ke"), ("Garissa", "ke"), ("Lamu", "ke"),
    # Tanzania
    ("Dar es Salaam", "tz"), ("Dodoma", "tz"), ("Arusha", "tz"), ("Mwanza", "tz"), ("Zanzibar", "tz"),
    ("Mbeya", "tz"), ("Morogoro", "tz"), ("Tanga", "tz"), ("Moshi", "tz"), ("Kigoma", "tz"), ("Iringa", "tz"),
    # Rwanda, Burundi, South Sudan, Ethiopia, Somalia, Djibouti, Eritrea, Sudan
    ("Kigali", "rw"), ("Huye", "rw"), ("Musanze", "rw"), ("Rubavu", "rw"), ("Bujumbura", "bi"), ("Gitega", "bi"),
    ("Juba", "ss"), ("Wau", "ss"), ("Addis Ababa", "et"), ("Dire Dawa", "et"), ("Hawassa", "et"), ("Bahir Dar", "et"),
    ("Mekelle", "et"), ("Gondar", "et"), ("Mogadishu", "so"), ("Hargeisa", "so"), ("Djibouti", "dj"),
    ("Asmara", "er"), ("Khartoum", "sd"), ("Port Sudan", "sd"),
    # DR Congo, Congo
    ("Kinshasa", "cd"), ("Lubumbashi", "cd"), ("Goma", "cd"), ("Bukavu", "cd"), ("Kisangani", "cd"),
    ("Mbuji-Mayi", "cd"), ("Kananga", "cd"), ("Matadi", "cd"), ("Kolwezi", "cd"), ("Likasi", "cd"), ("Uvira", "cd"),
    ("Bunia", "cd"), ("Butembo", "cd"), ("Beni", "cd"), ("Mbandaka", "cd"), ("Kikwit", "cd"),
    ("Brazzaville", "cg"), ("Pointe-Noire", "cg"),
    # Central
    ("Douala", "cm"), ("Yaoundé", "cm"), ("Bamenda", "cm"), ("Bafoussam", "cm"), ("Garoua", "cm"), ("Buea", "cm"),
    ("Limbe", "cm"), ("Libreville", "ga"), ("Port-Gentil", "ga"), ("Malabo", "gq"), ("Bangui", "cf"),
    ("N'Djamena", "td"),
    # Nigeria
    ("Lagos", "ng"), ("Abuja", "ng"), ("Port Harcourt", "ng"), ("Ibadan", "ng"), ("Kano", "ng"), ("Enugu", "ng"),
    ("Benin City", "ng"), ("Kaduna", "ng"), ("Jos", "ng"), ("Abeokuta", "ng"), ("Owerri", "ng"), ("Warri", "ng"),
    ("Calabar", "ng"), ("Uyo", "ng"), ("Onitsha", "ng"), ("Ilorin", "ng"), ("Asaba", "ng"), ("Akure", "ng"),
    ("Maiduguri", "ng"), ("Sokoto", "ng"),
    # Ghana, West Africa
    ("Accra", "gh"), ("Kumasi", "gh"), ("Tamale", "gh"), ("Takoradi", "gh"), ("Cape Coast", "gh"), ("Tema", "gh"),
    ("Abidjan", "ci"), ("Bouaké", "ci"), ("Yamoussoukro", "ci"), ("San-Pédro", "ci"), ("Dakar", "sn"),
    ("Touba", "sn"), ("Thiès", "sn"), ("Saint-Louis", "sn"), ("Ziguinchor", "sn"), ("Bamako", "ml"),
    ("Ouagadougou", "bf"), ("Bobo-Dioulasso", "bf"), ("Niamey", "ne"), ("Conakry", "gn"), ("Freetown", "sl"),
    ("Monrovia", "lr"), ("Banjul", "gm"), ("Serrekunda", "gm"), ("Bissau", "gw"), ("Lomé", "tg"), ("Cotonou", "bj"),
    ("Porto-Novo", "bj"), ("Nouakchott", "mr"), ("Praia", "cv"),
    # Southern
    ("Johannesburg", "za"), ("Cape Town", "za"), ("Durban", "za"), ("Pretoria", "za"), ("Soweto", "za"),
    ("Gqeberha", "za"), ("Bloemfontein", "za"), ("East London", "za"), ("Polokwane", "za"),
    ("Lusaka", "zm"), ("Ndola", "zm"), ("Kitwe", "zm"), ("Livingstone", "zm"), ("Harare", "zw"), ("Bulawayo", "zw"),
    ("Mutare", "zw"), ("Luanda", "ao"), ("Huambo", "ao"), ("Benguela", "ao"), ("Lobito", "ao"), ("Maputo", "mz"),
    ("Beira", "mz"), ("Nampula", "mz"), ("Lilongwe", "mw"), ("Blantyre", "mw"), ("Mzuzu", "mw"),
    ("Windhoek", "na"), ("Gaborone", "bw"), ("Maseru", "ls"), ("Mbabane", "sz"), ("Antananarivo", "mg"),
    ("Port Louis", "mu"), ("Moroni", "km"), ("Victoria", "sc"),
    # North
    ("Cairo", "eg"), ("Alexandria", "eg"), ("Casablanca", "ma"), ("Rabat", "ma"), ("Marrakech", "ma"),
    ("Algiers", "dz"), ("Tunis", "tn"), ("Tripoli", "ly"),
    # United States
    ("New York", "us"), ("Atlanta", "us"), ("Houston", "us"), ("Washington DC", "us"), ("Chicago", "us"),
    ("Los Angeles", "us"), ("Dallas", "us"), ("Minneapolis", "us"), ("Boston", "us"), ("Miami", "us"),
    ("Philadelphia", "us"), ("Charlotte", "us"), ("Seattle", "us"), ("Columbus", "us"), ("Baltimore", "us"),
    ("Detroit", "us"), ("Denver", "us"), ("Phoenix", "us"), ("Las Vegas", "us"), ("San Francisco", "us"),
    ("Oakland", "us"), ("New Orleans", "us"), ("Memphis", "us"), ("Nashville", "us"), ("Raleigh", "us"),
    ("Kansas City", "us"), ("Wichita", "us"), ("St. Louis", "us"), ("Indianapolis", "us"), ("Cleveland", "us"),
    ("Newark", "us"), ("Orlando", "us"), ("Tampa", "us"), ("Austin", "us"), ("San Antonio", "us"),
    ("Portland", "us"), ("Salt Lake City", "us"), ("Omaha", "us"), ("Des Moines", "us"), ("Milwaukee", "us"),
    # Canada
    ("Toronto", "ca"), ("Montreal", "ca"), ("Ottawa", "ca"), ("Calgary", "ca"), ("Edmonton", "ca"),
    ("Vancouver", "ca"), ("Winnipeg", "ca"), ("Halifax", "ca"), ("Quebec City", "ca"),
    # Europe
    ("London", "gb"), ("Manchester", "gb"), ("Birmingham", "gb"), ("Leeds", "gb"), ("Leicester", "gb"),
    ("Liverpool", "gb"), ("Bristol", "gb"), ("Glasgow", "gb"), ("Edinburgh", "gb"), ("Cardiff", "gb"),
    ("Dublin", "ie"), ("Paris", "fr"), ("Lyon", "fr"), ("Marseille", "fr"), ("Lille", "fr"), ("Bordeaux", "fr"),
    ("Toulouse", "fr"), ("Nantes", "fr"), ("Strasbourg", "fr"), ("Brussels", "be"), ("Antwerp", "be"),
    ("Liège", "be"), ("Amsterdam", "nl"), ("Rotterdam", "nl"), ("The Hague", "nl"), ("Berlin", "de"),
    ("Hamburg", "de"), ("Frankfurt", "de"), ("Cologne", "de"), ("Munich", "de"), ("Rome", "it"), ("Milan", "it"),
    ("Turin", "it"), ("Madrid", "es"), ("Barcelona", "es"), ("Lisbon", "pt"), ("Porto", "pt"), ("Geneva", "ch"),
    ("Zurich", "ch"), ("Stockholm", "se"), ("Oslo", "no"), ("Copenhagen", "dk"), ("Helsinki", "fi"), ("Vienna", "at"),
    # Middle East, Asia, Oceania
    ("Dubai", "ae"), ("Abu Dhabi", "ae"), ("Riyadh", "sa"), ("Jeddah", "sa"), ("Doha", "qa"), ("Kuwait City", "kw"),
    ("Muscat", "om"), ("Tel Aviv", "il"), ("Istanbul", "tr"), ("Guangzhou", "cn"), ("Mumbai", "in"),
    ("Tokyo", "jp"), ("Sydney", "au"), ("Melbourne", "au"), ("Perth", "au"), ("Brisbane", "au"), ("Auckland", "nz"),
    # Latin America
    ("Salvador", "br"), ("Rio de Janeiro", "br"), ("São Paulo", "br"), ("Recife", "br"), ("Belo Horizonte", "br"),
    ("Brasília", "br"), ("Fortaleza", "br"), ("São Luís", "br"), ("Porto Alegre", "br"), ("Belém", "br"),
    ("Cali", "co"), ("Cartagena", "co"), ("Bogotá", "co"), ("Medellín", "co"), ("Barranquilla", "co"),
    ("Quibdó", "co"), ("Buenaventura", "co"), ("Caracas", "ve"), ("Esmeraldas", "ec"), ("Guayaquil", "ec"),
    ("Quito", "ec"), ("Lima", "pe"), ("Mexico City", "mx"), ("Panama City", "pa"), ("Colón", "pa"),
    ("Limón", "cr"), ("San José", "cr"), ("La Ceiba", "hn"), ("Tegucigalpa", "hn"), ("San Pedro Sula", "hn"),
    ("Roatán", "hn"), ("Trujillo", "hn"), ("Belize City", "bz"), ("Dangriga", "bz"), ("Punta Gorda", "bz"),
    ("Livingston", "gt"), ("Guatemala City", "gt"), ("Bluefields", "ni"), ("Buenos Aires", "ar"),
    ("Montevideo", "uy"), ("Santiago", "cl"),
    # Caribbean
    ("Havana", "cu"), ("Santiago de Cuba", "cu"), ("Santo Domingo", "do"), ("Santiago de los Caballeros", "do"),
    ("Port-au-Prince", "ht"), ("Cap-Haïtien", "ht"), ("Kingston", "jm"), ("Montego Bay", "jm"),
    ("Port of Spain", "tt"), ("San Fernando", "tt"), ("Bridgetown", "bb"), ("Nassau", "bs"), ("San Juan", "pr"),
    ("Kingstown", "vc"), ("Castries", "lc"), ("St. George's", "gd"), ("St. John's", "ag"), ("Basseterre", "kn"),
    ("Roseau", "dm"), ("Georgetown", "gy"), ("Paramaribo", "sr"),
]
