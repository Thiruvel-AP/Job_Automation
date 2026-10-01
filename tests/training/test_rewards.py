from careerops.training.rewards import band_reward, faithfulness_reward


def test_rewards():
    kws = [{"term": "python", "weight": 1, "required": True}, {"term": "sql", "weight": 1, "required": True},
           {"term": "docker", "weight": 1, "required": True}, {"term": "aws", "weight": 1, "required": True}]
    r = band_reward(["python sql docker", "python sql docker aws", "nothing"], [kws] * 3)
    assert r[0] == 1.0 and r[1] < 1.0 and r[2] < r[1]          # 75% in band; 100% stuffed; 0% worst
    f = faithfulness_reward(["Used Docker on AWS.", "Used Kubernetes."], ["Used Docker on AWS."] * 2,
                            [[], []], [["kubernetes"]] * 2, [kws] * 2)
    assert f[0] > 0 > f[1]
