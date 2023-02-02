def can_be_split(n: int, v: bool = False) -> bool:
    def split_func(lst, sz): return [lst[i:i+sz]
                                    for i in range(0, len(lst), sz)]
    for players_per_game in range(6, 1, -1):
        if n % players_per_game == 0:
            if v:
                print(f"{players_per_game}ч x {n // players_per_game}ст; ", end="")
            result = split_func(range(n), players_per_game)
            return result if (can_be_split(len(result), v)) or len(result) == 1 else False
    if v:
        print(f"{n}ч x {n / 6}-{n / 2}ст")
    return False

for i in range(2, 101):
    print(f"{i}: ", end="")
    can_be_split(i, True)
    print()